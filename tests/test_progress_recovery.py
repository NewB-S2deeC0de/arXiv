from crawler.progress import ProgressStore

def test_recovery(tmp_path):
    s=ProgressStore(tmp_path)
    s.initialize(['2504.02523'])
    key=('2504.02523',0,'discovery',0)
    for _ in range(3): s.start_attempt(key)
    s=ProgressStore(tmp_path)
    assert s.get(key)['attempts']==3
    assert not s.pending('discovery')

def test_preserve_valid_backup(tmp_path):
    s=ProgressStore(tmp_path); s.initialize(['2504.02523']); s.save(with_views=True)
    s.path.write_text('corrupt')
    recovered=ProgressStore(tmp_path); recovered.save(with_views=True)
    recovered.path.write_text('another corruption')
    assert len(ProgressStore(tmp_path).rows)==2

def test_missing_primary_does_not_initialize_empty(tmp_path):
    import pytest
    s=ProgressStore(tmp_path); s.initialize(['2504.02523']); s.path.unlink()
    with pytest.raises(RuntimeError): ProgressStore(tmp_path)

def test_retry_failed_reference_rearms_exhausted_page(tmp_path):
    s=ProgressStore(tmp_path,s2_max_attempts=3); s.initialize(['2504.02523'])
    base=('2504.02523',0,'references',0); page=('2504.02523',0,'references',100)
    s.add(page)
    for _ in range(3): s.start_attempt(page)
    s.finish(base,'failed','attempts_exhausted')
    s.reset('failed')
    assert s.get(page)['attempts']==0
    assert s.get(page)['total']==3
