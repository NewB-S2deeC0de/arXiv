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
def test_traversal(tmp_path,name):
    import tarfile,io
    archive=tmp_path/'download'
    with tarfile.open(archive,'w:gz') as tar:
        member=tarfile.TarInfo(name); member.size=1; tar.addfile(member,io.BytesIO(b'x'))
    with pytest.raises(ValueError): inspect_and_extract(archive,tmp_path/'out',CrawlerConfig())

@pytest.mark.parametrize('kind',['symlink','hardlink'])
def test_links(tmp_path,kind):
    import tarfile
    archive=tmp_path/'download'
    with tarfile.open(archive,'w:gz') as tar:
        member=tarfile.TarInfo('bad.tex'); member.type=tarfile.SYMTYPE if kind=='symlink' else tarfile.LNKTYPE; member.linkname='../escape'; tar.addfile(member)
    with pytest.raises(ValueError): inspect_and_extract(archive,tmp_path/'out',CrawlerConfig())

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

def test_unsafe_directory_rejected(tmp_path):
    import tarfile
    p=tmp_path/'download'
    with tarfile.open(p,'w:gz') as t:
        m=tarfile.TarInfo('../outside'); m.type=tarfile.DIRTYPE; t.addfile(m)
    with pytest.raises(ValueError): inspect_and_extract(p,tmp_path/'out',CrawlerConfig())


def test_docx_and_unrecognized_zip(tmp_path):
    import zipfile
    archive=tmp_path/'source'
    with zipfile.ZipFile(archive,'w') as z:
        z.writestr('[Content_Types].xml','<Types><Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>')
        z.writestr('word/document.xml','<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"/>')
    result=inspect_and_extract(archive,tmp_path/'out',CrawlerConfig())
    assert result['status']=='no_tex'
    assert (tmp_path/'out/inventory.json').exists()
    with zipfile.ZipFile(archive,'w') as z: z.writestr('error.html','<html>blocked</html>')
    with pytest.raises(ValueError,match='Unrecognized'): inspect_and_extract(archive,tmp_path/'out',CrawlerConfig())


def test_docx_resource_limit(tmp_path):
    import zipfile
    archive=tmp_path/'source'
    with zipfile.ZipFile(archive,'w') as z: z.writestr('word/document.xml',b'x'*100)
    config=CrawlerConfig(); config.execution['max_member_bytes']=20
    with pytest.raises(RuntimeError,match='resource_limit'):
        inspect_and_extract(archive,tmp_path/'out',config)


def test_html_article_gzip_and_raw(tmp_path):
    import gzip
    html_content = b'<!DOCTYPE html><html><head><title>Workshop Paper</title></head><body><h1>Content</h1></body></html>'
    
    # Test gzip compressed HTML paper (e.g. arXiv-2504.07475v1.html.gz)
    gz_archive = tmp_path / 'paper.html.gz'
    with gzip.open(gz_archive, 'wb') as f:
        f.write(html_content)
    res_gz = inspect_and_extract(gz_archive, tmp_path / 'out_gz', CrawlerConfig())
    assert res_gz['status'] == 'no_tex'
    assert res_gz['no_tex_reason'] == 'valid_source_without_tex'
    assert res_gz['discarded_files'] == 1
    assert (tmp_path / 'out_gz' / 'inventory.json').exists()

    # Test raw uncompressed HTML paper
    raw_archive = tmp_path / 'paper.html'
    raw_archive.write_bytes(html_content)
    res_raw = inspect_and_extract(raw_archive, tmp_path / 'out_raw', CrawlerConfig())
    assert res_raw['status'] == 'no_tex'
    assert res_raw['no_tex_reason'] == 'valid_source_without_tex'


def test_html_article_resource_limit(tmp_path):
    import gzip
    html_content = b'<!DOCTYPE html><html><head><title>Big Paper</title></head><body>' + (b'x' * 500) + b'</body></html>'
    gz_archive = tmp_path / 'big.html.gz'
    with gzip.open(gz_archive, 'wb') as f:
        f.write(html_content)
    cfg = CrawlerConfig()
    cfg.execution['max_member_bytes'] = 50
    with pytest.raises(RuntimeError, match='resource_limit'):
        inspect_and_extract(gz_archive, tmp_path / 'out_limit', cfg)


def test_html_error_pages_rejected(tmp_path):
    # Short error snippet
    p1 = tmp_path / 'err1.html'
    p1.write_bytes(b'<html>Oops</html>')
    with pytest.raises(ValueError, match='Unrecognized'):
        inspect_and_extract(p1, tmp_path / 'out_err1', CrawlerConfig())

    # Full HTML structure but containing access denied / block error
    p2 = tmp_path / 'err2.html'
    p2.write_bytes(b'<!DOCTYPE html><html><head><title>403 Forbidden</title></head><body>Access Denied</body></html>')
    with pytest.raises(ValueError, match='Unrecognized'):
        inspect_and_extract(p2, tmp_path / 'out_err2', CrawlerConfig())

