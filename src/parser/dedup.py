"""Cross-version element deduplication with per-version hierarchy edges (C2)."""
from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Iterable

from .contract import (Element, ElementKind, HierarchyEdge, KIND_PREFIX,
                       TreeNode, publication_key)


class ElementStore:
    """One shared element store per publication.

    Content that is identical after cleanup is stored once and shared across
    versions. A repeated identical element inside the same version keeps a new
    id so its structure is not collapsed. ``normalize`` may be supplied by the
    caller to compare content after B1 cleanup; it never rewrites stored text.
    """

    def __init__(self, arxiv_id: str, normalize: Callable[[str], str] | None = None):
        self.arxiv_id = arxiv_id
        self.pubkey = publication_key(arxiv_id)
        self.normalize = normalize
        self.elements: dict[str, Element] = {}
        self.signature_to_id: dict[tuple[str, str], str] = {}
        self.version_signatures: dict[int, set[tuple[str, str]]] = defaultdict(set)
        self.counters: dict[str, int] = defaultdict(int)
        self.edges: list[HierarchyEdge] = []
        self.total_nodes = 0

    def _new_id(self, kind: ElementKind) -> str:
        prefix = KIND_PREFIX[kind]
        self.counters[prefix] += 1
        return f'{self.pubkey}#{prefix}{self.counters[prefix]}'

    def _signature(self, kind: ElementKind, content: str) -> tuple[str, str]:
        normalized = self.normalize(content) if self.normalize else content.strip()
        return str(kind), normalized

    def intern(self, version: int, kind: ElementKind, content: str) -> str:
        content = content or ''
        self.total_nodes += 1
        signature = self._signature(kind, content)
        if kind is not ElementKind.DOCUMENT:
            existing = self.signature_to_id.get(signature)
            if existing is not None and signature not in self.version_signatures[version]:
                self.version_signatures[version].add(signature)
                return existing
        element_id = self._new_id(kind)
        self.elements[element_id] = Element(element_id, self.arxiv_id, str(kind), content)
        if kind is not ElementKind.DOCUMENT and signature not in self.signature_to_id:
            self.signature_to_id[signature] = element_id
        self.version_signatures[version].add(signature)
        return element_id

    def add_tree(self, version: int, root: TreeNode) -> str:
        root_id = self.intern(version, ElementKind.DOCUMENT, root.content)
        self._add_children(version, root, root_id)
        return root_id

    def _add_children(self, version: int, node: TreeNode, parent_id: str) -> None:
        for position, child in enumerate(node.children):
            kind = ElementKind(child.kind)
            element_id = self.intern(version, kind, child.content)
            self.edges.append(HierarchyEdge(self.arxiv_id, version, element_id, parent_id, position))
            self._add_children(version, child, element_id)

    def sorted_elements(self) -> list[Element]:
        return [self.elements[key] for key in sorted(self.elements)]

    def sorted_edges(self) -> list[HierarchyEdge]:
        return sorted(self.edges, key=lambda edge: (edge.version, edge.parent_id, edge.position))

    @property
    def distinct_elements(self) -> int:
        return len(self.elements)


def deduplicate_trees(arxiv_id: str, version_trees: Iterable[tuple[int, TreeNode]],
                      normalize: Callable[[str], str] | None = None) -> ElementStore:
    """Deduplicate parsed trees for one publication across versions."""
    store = ElementStore(arxiv_id, normalize=normalize)
    for version, tree in sorted(version_trees, key=lambda item: item[0]):
        store.add_tree(version, tree)
    return store
