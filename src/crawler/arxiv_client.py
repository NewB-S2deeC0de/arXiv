import re
from defusedxml import ElementTree
from defusedxml.common import DefusedXmlException
from xml.etree.ElementTree import ParseError, Element, tostring

NS={'a':'http://www.w3.org/2005/Atom','x':'http://arxiv.org/schemas/atom'}

def parse_atom(raw,paper,version=None):
    try: 
        root=ElementTree.fromstring(raw)
    except (ParseError,DefusedXmlException) as exc: 
        raise ValueError('Invalid Atom XML') from exc
    if root.tag != '{http://www.w3.org/2005/Atom}feed': 
        raise ValueError('Expected Atom feed')
    entries = root.findall('a:entry',NS)
    if not entries:
        return {'not_found':True}
    if len(entries) != 1:
        raise ValueError('Unexpected Atom entry count')
    entry = entries[0]
    def text(name): 
        return entry.findtext(name,default=None,namespaces=NS)
    identifier = text('a:id') or ''
    match = re.search(r'/abs/(.+)v([1-9][0-9]*)$',identifier)
    if not match or match[1] != paper or (version and int(match[2]) != version):
        raise ValueError('Atom ID mismatch or error entry')
    journal_ref=text('x:journal_ref')
    return dict(arxiv_id=paper, version=int(match[2]), 
                title=text('a:title'), abstract=text('a:summary'), 
                authors=[a.findtext('a:name', namespaces = NS) 
                         for a in entry.findall('a:author',NS)] , 
                categories=[a.get('term') for a in entry.findall('a:category', NS)],
                published=text('a:published'),updated=text('a:updated'),
                journal_ref=journal_ref, doi=text('x:doi'), venue=journal_ref or None)

def parse_atom_batch(raw, requested_papers):
    """Map only verified entries to requested IDs; API errors are never absence."""
    requested=list(requested_papers)
    if not requested or len(set(requested))!=len(requested):
        raise ValueError('Expected distinct requested IDs')
    try: root=ElementTree.fromstring(raw)
    except (ParseError,DefusedXmlException) as exc: raise ValueError('Invalid Atom XML') from exc
    if root.tag!='{http://www.w3.org/2005/Atom}feed': raise ValueError('Expected Atom feed')
    entries=root.findall('a:entry',NS)
    total=root.findtext('{http://a9.com/-/spec/opensearch/1.1/}totalResults')
    if total is not None:
        try: count=int(total)
        except ValueError as exc: raise ValueError('Invalid totalResults') from exc
        if count!=len(entries): raise ValueError('Incomplete batch response')
    results={}
    for entry in entries:
        identifier=entry.findtext('a:id',default='',namespaces=NS)
        match=re.search(r'/abs/(.+)v([1-9][0-9]*)$',identifier)
        if not match: raise ValueError('Atom ID mismatch or error entry')
        paper=match[1]
        if paper not in requested: raise ValueError('Unrequested Atom entry')
        if paper in results: raise ValueError('Duplicate Atom entry')
        # Reuse the version-safe metadata parser rather than maintain two schemas.
        feed=Element('{http://www.w3.org/2005/Atom}feed')
        feed.append(entry)
        results[paper]=parse_atom(tostring(feed),paper)
    return {paper:results.get(paper,{'not_found':True,'arxiv_id':paper}) for paper in requested}


def parse_version_batch(raw, requested_versions):
    """Keep versioned identifiers as mapping keys, including v1/v2 of one paper."""
    requested=list(requested_versions)
    if not requested or len(set(requested))!=len(requested): raise ValueError('Expected distinct version IDs')
    if any(re.fullmatch(r'.+v[1-9][0-9]*',v) is None for v in requested): raise ValueError('Expected versioned IDs')
    try: root=ElementTree.fromstring(raw)
    except (ParseError,DefusedXmlException) as exc: raise ValueError('Invalid Atom XML') from exc
    if root.tag!='{http://www.w3.org/2005/Atom}feed': raise ValueError('Expected Atom feed')
    entries=root.findall('a:entry',NS)
    total=root.findtext('{http://a9.com/-/spec/opensearch/1.1/}totalResults')
    if total is not None:
        try: count=int(total)
        except ValueError as exc: raise ValueError('Invalid totalResults') from exc
        if count!=len(entries): raise ValueError('Incomplete batch response')
    results={}
    for entry in entries:
        match=re.search(r'/abs/(.+)v([1-9][0-9]*)$',entry.findtext('a:id',default='',namespaces=NS))
        if not match: raise ValueError('Atom ID mismatch or error entry')
        identifier=f'{match[1]}v{match[2]}'
        if identifier not in requested or identifier in results: raise ValueError('Unexpected or duplicate version entry')
        feed=Element('{http://www.w3.org/2005/Atom}feed'); feed.append(entry)
        results[identifier]=parse_atom(tostring(feed),match[1],int(match[2]))
    return {v:results.get(v,{'not_found':True,'arxiv_id':re.sub(r'v[1-9][0-9]*$','',v)}) for v in requested}
