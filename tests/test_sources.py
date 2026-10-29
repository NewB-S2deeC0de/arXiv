import pytest
from crawler.sources import inspect_and_extract
from crawler.config import CrawlerConfig

def test_html(tmp_path):
    archive=tmp_path/'download'; archive.write_bytes(b'<html>Oops</html>')
    with pytest.raises(ValueError): inspect_and_extract(archive,tmp_path/'out',CrawlerConfig())

def test_tex(tmp_path):
    archive=tmp_path/'download'; raw=b'\\documentclass{article}\n\\includegraphics{image}'
    archive.write_bytes(raw)
    result=inspect_and_extract(archive,tmp_path/'out',CrawlerConfig())
    assert result['status']=='succeeded'
    assert (tmp_path/'out'/'tex'/'source.tex').read_bytes()==raw

def test_corrupt_gzip(tmp_path):
    archive=tmp_path/'bad'; archive.write_bytes(b'\x1f\x8b\x08\x00'+b'\x00'*6+b'\xff'*20)
    with pytest.raises(ValueError): inspect_and_extract(archive,tmp_path/'out',CrawlerConfig())

@pytest.mark.parametrize('name',['../bad.tex','/bad.tex','dir/../../bad.tex'])
def test_traversal_skipped(tmp_path,name):
    import tarfile,io
    archive=tmp_path/'download'
    with tarfile.open(archive,'w:gz') as tar:
        member=tarfile.TarInfo(name); member.size=1; tar.addfile(member,io.BytesIO(b'x'))
    result=inspect_and_extract(archive,tmp_path/'out',CrawlerConfig())
    assert result['status']=='no_tex' and result['discarded_files']==1
    assert not (tmp_path/'bad.tex').exists()

@pytest.mark.parametrize('kind',['symlink','hardlink'])
def test_links_skipped(tmp_path,kind):
    import tarfile
    archive=tmp_path/'download'
    with tarfile.open(archive,'w:gz') as tar:
        member=tarfile.TarInfo('bad.tex'); member.type=tarfile.SYMTYPE if kind=='symlink' else tarfile.LNKTYPE; member.linkname='../escape'; tar.addfile(member)
    result=inspect_and_extract(archive,tmp_path/'out',CrawlerConfig())
    assert result['status']=='no_tex' and result['discarded_files']==1

def test_tar_bytes_bib_only_and_limit(tmp_path):
    import tarfile,io
    archive=tmp_path/'download'
    with tarfile.open(archive,'w:gz') as tar:
        for name,data in [('nested/references.bib',b'@article{x}\n'),('image.png',b'1234')]:
            member=tarfile.TarInfo(name); member.size=len(data); tar.addfile(member,io.BytesIO(data))
    c=CrawlerConfig(); result=inspect_and_extract(archive,tmp_path/'out',c)
    assert result['status']=='no_tex' and result['unpacked_bytes']==16
    assert (tmp_path/'out/tex/nested/references.bib').read_bytes()==b'@article{x}\n'
    c.execution['max_member_bytes']=3
    with pytest.raises(RuntimeError,match='resource_limit'): inspect_and_extract(archive,tmp_path/'out',c)

def test_unsafe_directory_skipped(tmp_path):
    import tarfile
    p=tmp_path/'download'
    with tarfile.open(p,'w:gz') as t:
        m=tarfile.TarInfo('../outside'); m.type=tarfile.DIRTYPE; t.addfile(m)
    result=inspect_and_extract(p,tmp_path/'out',CrawlerConfig())
    assert result['status']=='no_tex'
    assert not (tmp_path/'outside').exists()

def test_large_non_source_member_does_not_fail_version(tmp_path):
    import tarfile,io
    p=tmp_path/'download'
    tex=b'\\documentclass{article}\n'
    with tarfile.open(p,'w:gz') as t:
        for name,data in [('main.tex',tex),('huge.png',b'x'*100)]:
            m=tarfile.TarInfo(name); m.size=len(data); t.addfile(m,io.BytesIO(data))
    c=CrawlerConfig(); c.execution['max_member_bytes']=50
    result=inspect_and_extract(p,tmp_path/'out',c)
    assert result['status']=='succeeded' and result['tex_count']==1
    assert (tmp_path/'out/tex/main.tex').read_bytes()==tex

def test_duplicate_names_keep_first_member(tmp_path):
    import tarfile,io
    p=tmp_path/'download'
    with tarfile.open(p,'w:gz') as t:
        for data in (b'\\documentclass{article}\nfirst',b'\\documentclass{article}\nsecond'):
            m=tarfile.TarInfo('main.tex'); m.size=len(data); t.addfile(m,io.BytesIO(data))
    result=inspect_and_extract(p,tmp_path/'out',CrawlerConfig())
    assert result['status']=='succeeded' and result['discarded_files']==1
    assert b'first' in (tmp_path/'out/tex/main.tex').read_bytes()

@pytest.mark.parametrize('payload',[b'PK\x03\x04docx',b'%!PS-Adobe-3.0'])
def test_non_tex_document_form_is_empty_record(tmp_path,payload):
    archive=tmp_path/'download'; archive.write_bytes(payload)
    result=inspect_and_extract(archive,tmp_path/'out',CrawlerConfig())
    assert result['status']=='no_tex' and result['discarded_files']==1
