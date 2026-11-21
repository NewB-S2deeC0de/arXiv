import pytest
from crawler.ids import generate_ids
from crawler.config import load_config
from crawler.secrets import load_semantic_scholar_key

def test_range():
    ids=generate_ids('2025-04',2523,'2025-04',7522)
    assert len(ids)==5000
    assert (ids[0],ids[-1])==('2504.02523','2504.07522')

@pytest.mark.parametrize('a,b,c,d',[('2025-04',2,'2025-05',3),('2025-04',4,'2025-04',3)])
def test_invalid(a,b,c,d):
    with pytest.raises(ValueError): generate_ids(a,b,c,d)

def test_config(tmp_path):
    p=tmp_path/'config.yaml'
    p.write_text('paths:\n  persistent_root: /absolute\n')
    with pytest.raises(ValueError): load_config(p)

def test_key(monkeypatch):
    monkeypatch.setenv('SEMANTIC_SCHOLAR_API_KEY',' secret ')
    assert load_semantic_scholar_key()=='secret'

def test_colab_denied_is_not_absent(monkeypatch):
    import sys,types
    class Missing(Exception): pass
    class Denied(Exception): pass
    def get(name): raise Denied()
    userdata=types.SimpleNamespace(get=get,SecretNotFoundError=Missing,NotebookAccessError=Denied)
    monkeypatch.delenv('SEMANTIC_SCHOLAR_API_KEY',raising=False)
    monkeypatch.setitem(sys.modules,'google.colab',types.SimpleNamespace(userdata=userdata))
    with pytest.raises(RuntimeError,match='Enable notebook access'): load_semantic_scholar_key()
    def missing(name): raise Missing()
    userdata.get=missing
    assert load_semantic_scholar_key() is None

def test_resolve_s2_key(monkeypatch):
    from crawler.secrets import resolve_s2_key
    monkeypatch.delenv('SEMANTIC_SCHOLAR_API_KEY', raising=False)
    monkeypatch.delenv('MY_MEMBER_KEY', raising=False)

    # Fallback to default
    monkeypatch.setenv('SEMANTIC_SCHOLAR_API_KEY', 'default-val')
    assert resolve_s2_key(None) == 'default-val'
    assert resolve_s2_key('') == 'default-val'

    # Env var name resolution
    monkeypatch.setenv('MY_MEMBER_KEY', 'member-key-123')
    assert resolve_s2_key('MY_MEMBER_KEY') == 'member-key-123'

    # Literal key string resolution
    assert resolve_s2_key('literal-key-abc') == 'literal-key-abc'

