import json
import pytest
from crawler.progress import ProgressStore
from crawler.storage import Storage,atomic_json,digest

def test_commit_replaces_stale_file_set(tmp_path):
    storage=Storage(tmp_path); key=('2504.02523',1,'source',0)
    stage=tmp_path/'stage'; (stage/'tex').mkdir(parents=True)
    (stage/'tex/old.tex').write_text('old'); atomic_json(stage/'inventory.json',{'tex_count':1})
    storage.commit(key,stage)
    (stage/'tex/old.tex').unlink(); atomic_json(stage/'inventory.json',{'tex_count':0})
    storage.commit(key,stage,'no_tex')
    assert not (storage.result_dir(key)/'tex/old.tex').exists()
    assert storage.verify(key)['status']=='no_tex'

def test_existing_results_without_progress_fail_closed(tmp_path):
    (tmp_path/'data/2504.02523').mkdir(parents=True)
    with pytest.raises(RuntimeError): ProgressStore(tmp_path)

def test_committed_third_attempt_page_can_resume(tmp_path):
    store=ProgressStore(tmp_path,s2_max_attempts=3); store.initialize(['2504.02523'])
    base=('2504.02523',0,'references',0)
    for _ in range(3): store.start_attempt(base)
    directory=store.storage.paper_dir(base[0])/'reference-staging'/'fingerprint'
    directory.mkdir(parents=True)
    raw=directory/'page-000000.json'; raw.write_text('{"data":[],"next":100}')
    atomic_json(directory/'page-000000.complete.json',{'sha256':digest(raw),'fingerprint':'fingerprint'})
    store.reconcile()
    assert store.get(base)['status']=='pending'

def test_invalid_succeeded_page_rearmed_on_explicit_retry(tmp_path):
    store=ProgressStore(tmp_path); store.initialize(['2504.02523'])
    base=('2504.02523',0,'references',0); page=('2504.02523',0,'references',100)
    store.add(page)
    for _ in range(3): store.start_attempt(page)
    store.finish(page,'succeeded'); store.finish(base,'failed','missing_page')
    store.reset('failed')
    assert store.get(page)['attempts']==0
    assert store.get(page)['total']==3
