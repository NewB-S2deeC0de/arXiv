# Milestone 1 completion ledger

Specification: ../milestone-1-crawler-implementation.md

## Historical initial bootstrap (superseded by later passes)

User smoke: 10 discoveries, 14 version metadata, 10 reference snapshots,
7 source successes, 2 source HTTP 429 failures, 5 payload_or_transport failures.
429 is confirmed server throttling. The other errors cannot be reconstructed
from the legacy error code; preserve staged payloads and provide offline diagnosis.

Initial remaining work (historical): precise diagnostics; CSV migration; recovery and pagination;
models/config; scheduler/redirects; source guards; metrics/export;
full-range CLI, refresh, notebook and comprehensive offline verification.


## Performance pass after cda2e1e

- Added exact version-ID metadata batching, preserving nullable dates and raw XML.
- Startup/resume owns full reconciliation; live metadata commits update only their jobs.
- Snapshot + hash-chained fsynced delta journal replaces full CSV rewrites per request.
- Recovery covers torn tail, checksum corruption, missing journal, legacy CSV,
  backup replay and pre-send write failure. No journal rotation: legacy backups
  must retain the full chain. This costs journal storage, avoids unsafe compaction.
- Disk baseline + indexed changed-subtree updates; successful staging removed.
- YAML and default config require S2 key; anonymous smoke requires explicit opt-in.
- Final review: fixed directory fsync capability reporting, metadata not_found
  recheck, and retained reference-page disk counts. Each has regression coverage.
- Ruling: Keep final TeX/Bib directory layout rather than package each version;
  preserve downstream/data compatibility. Small-file Drive overhead still needs
  live measurement; changing the persistence format is a separate migration.
- Live Colab/API performance verification and full-range crawl were not run.

- Final verification: `python -m pytest -q` → 78 passed; both notebook code cells
  compile with top-level await and have empty outputs; `git diff --check` clean.


## Cross-month range and safe upgrade

- Optional monthly_end_ids supplies verified last serials for every non-final
  month. No automatic 99999 assumption or hidden API discovery. Same-month
  default remains exactly 2504.02523–2504.07522; no progress schema changes.
- Tests cover inclusive month/year rollover, invalid/missing bounds, YAML load,
  and cross-year pipeline resume without repeat HTTP calls.
- Safe-stop guide keeps the current VM alive, backs up full runtime to local
  ZIP and downloads it before restart, then validates progress/cloud sync.
- Review correction: absence of active_run.json alone is not proof of successful
  finalization/checkpoint. Documentation now requires checking errors and progress.
- Ruling: Require explicit monthly bounds rather than enumerate 99999 serials;
  callers must obtain actual month boundaries from their assignment/verified
  source. This avoids huge unintended crawls but requires extra cross-month config.
