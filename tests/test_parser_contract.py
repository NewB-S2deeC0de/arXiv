from parser.contract import (
    BIBTEX_SCHEMA,
    ELEMENTS_SCHEMA,
    HIERARCHY_SCHEMA,
    BibEntry,
    Element,
    ElementKind,
    GatherDiagnostic,
    GatheredSource,
    HierarchyEdge,
    KIND_PREFIX,
    ParseDiagnostic,
    ParsedDocument,
    TreeNode,
    bibtex_rows,
    element_rows,
    hierarchy_rows,
    publication_key,
)


def test_publication_key_handles_new_and_old_style_ids():
    assert publication_key('2410.23025') == '2410-23025'
    assert publication_key('hep-th/9901001') == 'hep-th-9901001'


def test_element_kind_prefixes_match_specification():
    assert KIND_PREFIX[ElementKind.DOCUMENT] == 'root'
    assert KIND_PREFIX[ElementKind.HEADING] == 'h'
    assert KIND_PREFIX[ElementKind.PARAGRAPH] == 'p'
    assert KIND_PREFIX[ElementKind.LIST] == 'l'
    assert KIND_PREFIX[ElementKind.ITEM] == 'i'
    assert KIND_PREFIX[ElementKind.SENTENCE] == 's'
    assert KIND_PREFIX[ElementKind.BLOCK_FORMULA] == 'm'
    assert KIND_PREFIX[ElementKind.FIGURE] == 'f'


def test_table_schemas_match_contract_columns():
    assert ELEMENTS_SCHEMA.names == ['element_id', 'arxiv_id', 'kind', 'content']
    assert HIERARCHY_SCHEMA.names == ['arxiv_id', 'version', 'element_id', 'parent_id']
    assert BIBTEX_SCHEMA.names == [
        'arxiv_id', 'citation_key', 'entry_type', 'title', 'authors',
        'year', 'doi', 'journal', 'url',
    ]
    assert not ELEMENTS_SCHEMA.field('element_id').nullable
    assert not ELEMENTS_SCHEMA.field('arxiv_id').nullable
    assert not HIERARCHY_SCHEMA.field('element_id').nullable
    assert not BIBTEX_SCHEMA.field('citation_key').nullable


def test_row_converters_preserve_nulls_and_author_lists():
    element = Element('2410-23025#p1', '2410.23025', 'paragraph', 'Text')
    edge = HierarchyEdge('2410.23025', 2, '2410-23025#p1', '2410-23025#root1')
    entry = BibEntry('2410.23025', 'key', title=None, authors=['A', 'B'], year=None)

    assert element_rows([element]) == [dict(
        element_id='2410-23025#p1', arxiv_id='2410.23025',
        kind='paragraph', content='Text',
    )]
    assert hierarchy_rows([edge]) == [dict(
        arxiv_id='2410.23025', version=2,
        element_id='2410-23025#p1', parent_id='2410-23025#root1',
    )]
    row = bibtex_rows([entry])[0]
    assert row['title'] is None and row['year'] is None
    assert row['authors'] == ['A', 'B']


def test_shared_tree_and_gathered_types_are_plain_data():
    child = TreeNode('sentence', 'Text.')
    root = TreeNode('document', 'Title', [child])
    diagnostic = GatherDiagnostic('missing_include', 'Missing child.tex', 'warning',
                                  'main.tex', 10, 20)
    gathered = GatheredSource('body', 'partial', 'main.tex', ('main.tex',),
                              ('refs.bib',), (diagnostic,))
    parsed = ParsedDocument(root, 'partial', (
        ParseDiagnostic('uncertain_references', 'References kept', 'warning',
                        'main.tex', 1, 2),
    ))

    assert root.children == [child]
    assert gathered.text == 'body'
    assert gathered.status == 'partial'
    assert gathered.files == ('main.tex',)
    assert gathered.bib_files == ('refs.bib',)
    assert gathered.diagnostics[0].code == 'missing_include'
    assert gathered.diagnostics[0].path == 'main.tex'
    assert gathered.diagnostics[0].start == 10 and gathered.diagnostics[0].end == 20
    assert parsed.tree is root and parsed.status == 'partial'
    assert parsed.diagnostics[0].code == 'uncertain_references'
