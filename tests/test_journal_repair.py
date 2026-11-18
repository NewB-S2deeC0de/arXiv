from pathlib import Path
import json
import pytest
from crawler.progress import ProgressStore
from crawler.journal import repair_journal_sequence, inspect_journal_sequence

def test_inspect_and_repair_journal_sequence(tmp_path):
    store = ProgressStore(tmp_path)
    store.initialize(['2504.02523'])
    key = ('2504.02523', 0, 'discovery', 0)
    for _ in range(3):
        store.start_attempt(key)
    store.save(with_views=True)

    # Artificially inflate journal_sequence in jobs.csv.json and jobs.bak.json to simulate Drive sync lag
    meta_path = tmp_path / 'progress' / 'jobs.csv.json'
    meta = json.loads(meta_path.read_text())
    true_seq = meta['journal_sequence']
    meta['journal_sequence'] = true_seq + 10
    meta_path.write_text(json.dumps(meta))
    bak_meta = tmp_path / 'progress' / 'jobs.bak.json'
    if bak_meta.exists():
        bm = json.loads(bak_meta.read_text())
        bm['journal_sequence'] = true_seq + 10
        bak_meta.write_text(json.dumps(bm))

    # Before repair, loading ProgressStore must fail closed
    with pytest.raises(RuntimeError, match='missing committed records'):
        ProgressStore(tmp_path)

    # Run repair
    result = repair_journal_sequence(tmp_path)
    assert result['repaired'] is True
    assert result['target_sequence'] == true_seq

    # After repair, ProgressStore must load cleanly
    reopened = ProgressStore(tmp_path)
    assert reopened.get(key)['attempts'] == 3

def test_repair_empty_journal(tmp_path):
    # If no journal exists or empty
    result = repair_journal_sequence(tmp_path)
    assert result['repaired'] is False
    assert result['target_sequence'] == 0
