import asyncio
import threading
import pytest
from crawler.workers import ThreadWorker


def test_fifo_ack_backpressure_and_event_loop():
    async def run():
        entered=threading.Event(); release=threading.Event(); seen=[]
        def first():
            entered.set(); release.wait(5); seen.append(1)
        async with ThreadWorker(capacity=1, max_bytes=4) as worker:
            ack=await worker.submit(first, weight=4)
            while not entered.is_set(): await asyncio.sleep(.001)
            second=asyncio.create_task(worker.submit(lambda:seen.append(2),weight=1))
            await asyncio.sleep(.02)
            assert not second.done() and not ack.done()
            assert worker.used_bytes==4
            release.set()
            await ack; await (await second)
        assert seen==[1,2]
    asyncio.run(run())


def test_failure_wakes_blocked_submitters():
    async def run():
        gate=threading.Event()
        def fail(): gate.wait(5); raise OSError('disk failure')
        worker=ThreadWorker(capacity=1,max_bytes=4)
        await worker.__aenter__()
        ack=await worker.submit(fail,weight=4)
        waiting=asyncio.create_task(worker.submit(lambda:None,weight=1))
        gate.set()
        with pytest.raises(OSError): await ack
        with pytest.raises(OSError): await asyncio.wait_for(waiting,1)
        with pytest.raises(OSError): await worker.close()
    asyncio.run(run())


def test_cancellation_joins_writer_and_drains_accepted_work():
    async def run():
        gate=threading.Event(); entered=threading.Event(); finished=[]
        def slow(): entered.set(); gate.wait(5); finished.append(True)
        async def owner():
            async with ThreadWorker(capacity=2) as worker:
                await worker.call(slow)
        task=asyncio.create_task(owner())
        while not entered.is_set(): await asyncio.sleep(.001)
        task.cancel(); await asyncio.sleep(.01); task.cancel()
        await asyncio.sleep(.01)
        assert not task.done()
        gate.set()
        with pytest.raises(asyncio.CancelledError): await task
        assert finished==[True]
    asyncio.run(run())


def test_oversize_does_not_deadlock():
    async def run():
        async with ThreadWorker(capacity=1,max_bytes=4) as worker:
            with pytest.raises(ValueError,match='budget'): await worker.submit(lambda:None,weight=5)
            assert await worker.call(lambda:42)==42
    asyncio.run(run())
