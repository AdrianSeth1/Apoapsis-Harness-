# Controlled resilient-download evaluation repository (scenario v2)

This is the second scenario in the `download-service` evaluation fixture
family (see ADR 0012 and ADR 0028), used only by `apoapsis eval-planning` to
compare a monolithic request against an approved, plan-then-execute
decomposition. It is a separate, physically independent repository from
`examples/download-service` (the original resumable-download scenario, ADR
0012): the original scenario's fixture, visible checks, held-out oracle, and
historical evaluation path are preserved byte-for-byte and are never touched
by this one.

Unlike the original single-slice scenario, this task has three genuine
architecture boundaries with a real dependency:

- **Slice A** (`src/download_service_v2/jobs.py`): durable job-record
  bookkeeping -- attempt count, transferred bytes, an expected checksum, a
  lifecycle state, and failure information. Independent of B.
- **Slice B** (`src/download_service_v2/downloader.py`): a resilient
  downloader -- resumable via a `Range` request, deterministic retry with
  backoff on transient transport failures, and structured progress
  reporting via a callback. Independent of A. Uses an injectable clock/sleep
  and a fake transport in tests; never sleeps or touches the network for
  real.
- **Slice C** (`src/download_service_v2/service.py`): integrates A and B --
  persists progress and attempt state through a real download, verifies the
  downloaded content's SHA-256 checksum before reporting completion, and
  leaves a consistent failure state (with a reason) otherwise. Depends on
  both A and B.

Each slice has its own agent-visible development test
(`tests/test_jobs_contract.py`, `tests/test_resilient_downloader.py`) and
Slice C additionally has a model-visible integration acceptance test
(`tests/test_service_integration_visible.py`). A separate, held-out
cross-slice oracle (`tests/test_v2_holdout_acceptance.py`) is excluded from
every agent-visible fixture copy and is run only after normal verification
already reports completion.

Initialize it as its own repository before an evaluation:

```bash
git init -b main
git add .
git commit -m "Controlled resilient-download v2 baseline"
python -m unittest discover -s tests -v
```
