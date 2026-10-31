"""Bounded FIFO I/O workers with acknowledgement and cancellation-safe draining.

Only the consumer invokes filesystem code. Capacity/byte reservations include
active work, and are released after the operation (not when it is dequeued).
"""
import asyncio
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from functools import partial


async def join(task):
    """Join without cancelling a running filesystem operation, even on repeated Stop."""
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
    result = task.result()
    if cancelled:
        raise asyncio.CancelledError
    return result


class ThreadWorker:
    def __init__(self, capacity=100, max_bytes=None, name='filesystem'):
        if capacity < 1 or (max_bytes is not None and max_bytes < 1):
            raise ValueError('Worker budgets must be positive')
        self.capacity = capacity
        self.max_bytes = max_bytes
        self.name = name
        self.used_bytes = 0
        self.used_items = 0
        self.peak_bytes = 0
        self.peak_items = 0
        self._queue = deque()
        self._condition = asyncio.Condition()
        self._closing = False
        self._error = None

    async def __aenter__(self):
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix=self.name)
        self._task = asyncio.create_task(self._consume(), name=self.name)
        return self

    async def submit(self, function, *args, weight=0, **kwargs):
        if weight < 0 or (self.max_bytes is not None and weight > self.max_bytes):
            raise ValueError('Item exceeds worker byte budget')
        async with self._condition:
            await self._condition.wait_for(lambda: self._error or self._closing or (
                self.used_items < self.capacity and
                (self.max_bytes is None or self.used_bytes + weight <= self.max_bytes)))
            if self._error:
                raise self._error
            if self._closing:
                raise RuntimeError('Worker is closing')
            future = asyncio.get_running_loop().create_future()
            # A cancelled producer may no longer await its accepted item.
            future.add_done_callback(lambda f: f.exception() if not f.cancelled() else None)
            self._queue.append((partial(function, *args, **kwargs), weight, future))
            self.used_items += 1
            self.used_bytes += weight
            self.peak_items = max(self.peak_items, self.used_items)
            self.peak_bytes = max(self.peak_bytes, self.used_bytes)
            self._condition.notify_all()
            return future

    async def call(self, function, *args, **kwargs):
        future = await self.submit(function, *args, **kwargs)
        return await asyncio.shield(future)

    async def _consume(self):
        while True:
            async with self._condition:
                await self._condition.wait_for(lambda: self._queue or self._closing)
                if not self._queue:
                    return
                function, weight, future = self._queue.popleft()
            try:
                result = await asyncio.get_running_loop().run_in_executor(self._executor, function)
            except BaseException as exc:
                async with self._condition:
                    self._error = exc
                    future.set_exception(exc)
                    while self._queue:
                        _, _, pending = self._queue.popleft()
                        pending.set_exception(exc)
                    self.used_items = self.used_bytes = 0
                    self._condition.notify_all()
                return
            else:
                async with self._condition:
                    future.set_result(result)
                    self.used_items -= 1
                    self.used_bytes -= weight
                    self._condition.notify_all()

    async def close(self):
        async with self._condition:
            self._closing = True
            self._condition.notify_all()
        try:
            await join(self._task)
        finally:
            self._executor.shutdown(wait=True)
        if self._error:
            raise self._error

    async def __aexit__(self, exc_type, exc, tb):
        await self.close()
