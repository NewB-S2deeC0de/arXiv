from parser.contract import TreeNode
from parser.dedup import ElementStore, deduplicate_trees

ARXIV = '2410.23025'
PUBKEY = '2410-23025'


def _tree(*paragraphs: str) -> TreeNode:
    root = TreeNode('document', 'Title')
    root.children = [TreeNode('paragraph', text) for text in paragraphs]
    return root


def _paragraphs(store: ElementStore):
    return [element for element in store.elements.values() if element.kind == 'paragraph']


def test_dedup_reuses_content_across_versions_but_not_within_one():
    store = ElementStore(ARXIV)
    first = store.add_tree(1, _tree('Shared sentence.'))
    second = store.add_tree(2, _tree('Shared sentence.', 'Shared sentence.'))
    assert first != second
    graph = {edge.parent_id: edge.element_id for edge in store.edges if edge.version == 1}
    assert graph[first].startswith(f'{PUBKEY}#p')
    shared_id = graph[first]
    ids_v2 = [edge.element_id for edge in store.edges if edge.version == 2 and edge.parent_id == second]
    assert ids_v2[0] == shared_id
    assert ids_v2[1] != shared_id
    assert store.total_nodes == 5
    assert store.distinct_elements == 4


def test_dedup_keeps_document_roots_per_version():
    store = ElementStore(ARXIV)
    first = store.add_tree(1, _tree('Same.'))
    second = store.add_tree(2, _tree('Same.'))
    assert store.elements[first].kind == 'document'
    assert store.elements[second].kind == 'document'
    assert first != second


def test_dedup_can_compare_after_b1_normalization():
    store = ElementStore(ARXIV, normalize=lambda text: ' '.join(text.split()))
    store.add_tree(1, _tree('Spaced   text.'))
    store.add_tree(2, _tree('Spaced text.'))
    paragraphs = _paragraphs(store)
    assert len(paragraphs) == 1
    assert paragraphs[0].content == 'Spaced   text.'


def test_dedup_shares_content_under_different_parents():
    store = ElementStore(ARXIV)
    first = TreeNode('document', 'Title')
    heading_one = TreeNode('heading', 'Introduction')
    heading_one.children.append(TreeNode('paragraph', 'Shared.'))
    first.children.append(heading_one)
    second = TreeNode('document', 'Title')
    heading_two = TreeNode('heading', 'Method')
    heading_two.children.append(TreeNode('paragraph', 'Shared.'))
    second.children.append(heading_two)

    store.add_tree(1, first)
    store.add_tree(2, second)

    paragraphs = _paragraphs(store)
    assert len(paragraphs) == 1
    paragraph_id = paragraphs[0].element_id
    headings = {element_id for element_id, element in store.elements.items()
                if element.kind == 'heading'}
    edges = [edge for edge in store.edges if edge.element_id == paragraph_id]
    assert {edge.parent_id for edge in edges} == headings


def test_dedup_does_not_merge_different_kinds():
    store = ElementStore(ARXIV)
    first = TreeNode('document', 'Title', [TreeNode('paragraph', 'Same.')])
    second = TreeNode('document', 'Title', [TreeNode('sentence', 'Same.')])
    store.add_tree(1, first)
    store.add_tree(2, second)
    assert {element.kind for element in store.elements.values()} == {
        'document', 'paragraph', 'sentence',
    }
    assert len(_paragraphs(store)) == 1
    assert len([element for element in store.elements.values() if element.kind == 'sentence']) == 1


def test_dedup_is_scoped_to_one_publication():
    first = deduplicate_trees('2410.23025', [(1, _tree('Same.'))])
    second = deduplicate_trees('2410.23026', [(1, _tree('Same.'))])
    assert set(first.elements).isdisjoint(second.elements)
    assert {element.arxiv_id for element in first.elements.values()} == {'2410.23025'}
    assert {element.arxiv_id for element in second.elements.values()} == {'2410.23026'}


def test_dedup_empty_publication_has_no_elements_or_edges():
    store = deduplicate_trees(ARXIV, [])
    assert store.total_nodes == 0
    assert store.distinct_elements == 0
    assert store.sorted_elements() == []
    assert store.sorted_edges() == []


def test_dedup_output_is_deterministic():
    trees = [(1, _tree('One.', 'Shared.')), (2, _tree('Two.', 'Shared.'))]
    forward = deduplicate_trees(ARXIV, trees)
    backward = deduplicate_trees(ARXIV, reversed(trees))
    assert [element.element_id for element in forward.sorted_elements()] == [
        element.element_id for element in backward.sorted_elements()
    ]
    assert [(edge.version, edge.element_id, edge.parent_id) for edge in forward.sorted_edges()] == [
        (edge.version, edge.element_id, edge.parent_id) for edge in backward.sorted_edges()
    ]


def test_edges_are_sorted_and_parents_exist():
    store = ElementStore(ARXIV)
    store.add_tree(2, _tree('Two.'))
    store.add_tree(1, _tree('One.'))
    edges = store.sorted_edges()
    assert [edge.version for edge in edges] == [1, 2]
    ids = set(store.elements)
    assert all(edge.parent_id in ids and edge.element_id in ids for edge in edges)
