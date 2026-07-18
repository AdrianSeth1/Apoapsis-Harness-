# Apoapsis 1.0 — 64k vs 128k profile evidence (three attempts per profile)

- Date: 2026-07-18
- Scope: Priority A ("evidence before more retrieval machinery"), extended
  to three attempts per profile. Six real
  `apoapsis eval download-service --lane local` runs (three at
  `--context-profile 64k`, three at `--context-profile 128k`), aggregated
  with `apoapsis eval-aggregate` (file-based, no model calls). Retrieval
  architecture was **not** changed for this evidence pass.
- Model: `qwen3-coder-next:q4_K_M` via native loopback Ollama, temperature 0,
  on the reference machine (RTX 4090, 24GB VRAM; 64GB RAM).
- Commands:
  ```bash
  apoapsis eval download-service --lane local --context-profile 64k  --output-dir .apoapsis-eval/priority-a-64k
  apoapsis eval download-service --lane local --context-profile 64k  --output-dir .apoapsis-eval/priority-a-64k-run2
  apoapsis eval download-service --lane local --context-profile 64k  --output-dir .apoapsis-eval/priority-a-64k-run3
  apoapsis eval download-service --lane local --context-profile 128k --output-dir .apoapsis-eval/priority-a-128k
  apoapsis eval download-service --lane local --context-profile 128k --output-dir .apoapsis-eval/priority-a-128k-run2
  apoapsis eval download-service --lane local --context-profile 128k --output-dir .apoapsis-eval/priority-a-128k-run3
  apoapsis eval-aggregate .apoapsis-eval/priority-a-{64k,64k-run2,64k-run3,128k,128k-run2,128k-run3}/comparison.json --output-dir .apoapsis-eval/priority-a-aggregate-full
  ```

## Per-attempt results

| Profile | Attempt | Reached context compilation | Outcome (own verification) | Held-out oracle | Calls | Latency s |
| --- | --- | --- | --- | --- | ---: | ---: |
| 64k | 1 | yes | complete | **failed** (false success) | 9 | 147.0 |
| 64k | 2 | yes | complete | **failed** (false success) | 6 | 88.5 |
| 64k | 3 | yes | complete | **passed** (true success) | 9 | 94.6 |
| 128k | 1 | **no** | failed (specification error) | not run | 1 | 48.0 |
| 128k | 2 | yes | complete | **failed** (false success) | 7 | 116.1 |
| 128k | 3 | yes | complete | **failed** (false success) | 7 | 84.7 |

**End-to-end failures** (counting the specification failure as a failure,
per instruction): 1/6, all at 128k attempt 1.

**Reached context compilation, reported separately from end-to-end
outcome**: 5/6 (all except 128k attempt 1) — 3/3 at 64k, 2/3 at 128k. Given
only one specification failure occurred, and it happened at 128k rather
than 64k, this sample **does not show a profile-specific or repeatable
128k-before-retrieval failure** — with n=3 per profile, 0/3 vs 1/3 is not
strong evidence that 128k itself causes the specification-extraction error.
It looks like general specification-extraction flakiness (see "Specification
reliability" below), not something tied to context width. Context quality
*is* measurable here: 5 of 6 attempts did reach and use real repository
retrieval.

Aggregate (`apoapsis eval-aggregate`, `EVAL-AGG-98C10812F0AB`, 6 attempts):

| Metric | Value | Denominator |
| --- | --- | --- |
| Local-only verified completion | 83.3% | 5/6 |
| Overall verified completion | 83.3% | 5/6 |
| Human review | 0.0% | 0/6 |
| Unsafe-patch rejection | 0.0% | 0/10 patch attempts |
| **False success** | **80.0%** | **4/5** (of 5 oracle-evaluated completions) |
| 64k completion / median latency | 100.0% (3/3) | 92.9 s median, 144.7 s p95 |
| 128k completion / median latency | 66.7% (2/3) | 83.3 s median, 114.9 s p95 |
| Frontier rescue / hosted savings / local-vs-one-shot | unmeasured | no hosted-frontier or one-shot lane run in this comparison |

## Take the 64k result seriously: the model is not unusable, but it is unreliable in a specific, identifiable way

Across all three 64k attempts, the deterministic context compiler found
exactly the same two relevant files (`downloader.py`, `jobs.py`) immediately
and consistently — retrieval was never the variable. What varied, run to
run, was the model's patch:

**Attempt 2 (false success)** — handles the simple resume case but not the
edge cases:
```python
resume_offset = self.jobs.get_offset(url)
headers = {}
if resume_offset > 0:
    headers["Range"] = f"bytes={resume_offset}-"
response = self.transport.get(url, headers=headers)
...
downloaded = 0
with destination.open("ab") as handle:
    for chunk in response.iter_chunks():
        handle.write(chunk)
        downloaded += len(chunk)
        self.jobs.set_offset(url, resume_offset + downloaded)
return downloaded
```
Always appends (`"ab"`), never checking whether the server actually honored
the Range request — a server that ignores it and returns a fresh `200`
gets its stale partial data left in place. `return downloaded` also returns
only the bytes fetched *this call*, not the true total.

**Attempt 3 (true success — the same model, same task, same context, just a
different sample)**:
```python
start_offset = self.jobs.get_offset(url)
headers = {}
if start_offset > 0:
    headers["Range"] = f"bytes={start_offset}-"
response = self.transport.get(url, headers=headers)
...
downloaded = start_offset
mode = "ab" if start_offset > 0 else "wb"
with destination.open(mode) as handle:
    if response.status_code == 200 and start_offset > 0:
        handle.seek(0)
        handle.truncate()
        downloaded = 0
    for chunk in response.iter_chunks():
        handle.write(chunk)
        downloaded += len(chunk)
```
This one explicitly branches on `response.status_code`, truncates stale
partial data when the server ignores the range, and initializes `downloaded`
from `start_offset` so the return value is the true total. Correct, on both
counts the other attempts got wrong.

A 128k attempt (run 2) is a useful third data point: it got the
stale-data-truncation branch right (`mode = "wb"` when `offset > 0 and
response.status_code == 200`) but still returned only the new bytes instead
of the total — showing the two defects are somewhat independent and the
model doesn't reliably get both right together (only 1 of 6 total attempts
across both profiles did).

**The conclusion to take seriously**: this is not "the model is bad" — it
solved the hard part (conditional Range headers, append-vs-write mode) in
every attempt, and got the full correct answer in 1 of 6. It is "ordinary
verification cannot tell these apart," because the two acceptance tests
that would have caught it are exactly the tests withheld from the agent's
own visible verification by design (ADR 0012's held-out oracle). Without
that oracle, 4 of these 6 runs would have been recorded as clean successes.
The held-out oracle worked exactly as intended — this is the mechanism
doing its job, not a discouraging result about the harness.

## Specification reliability (separate from context quality, not investigated further here)

128k attempt 1 failed before context compilation ran at all: the model's
drafted specification attached a `verification_method` field to
`acceptance_criteria` entries, a field `AcceptanceCriterion` does not define
(only `HardConstraint` does), so strict schema validation correctly
rejected it. This happened in 1 of 6 runs, at 128k, not at 64k — with this
sample size that is not strong evidence the failure is 128k-specific rather
than general model/sampling flakiness in specification extraction. It is
flagged here as a distinct reliability question worth its own investigation
(e.g., whether the extraction prompt sufficiently disambiguates the two
schemas, or whether a retry-with-feedback policy on a validation error would
help) — separate from, and not blocking, context-quality measurement, since
5 of 6 runs did reach real retrieval.

## Diagnosis against the "no embeddings" precondition

Per the explicit precondition, embeddings/learned ranking/model-selected
context are only justified by a **repeatable** failure of the deterministic
lexical/symbol/import/test/diff retrieval path, with evidence identifying
why. Across all 5 attempts that reached retrieval, the compiler found the
same correct two files every time, with stable, correctly-scoped evidence
(per `ContextMeasurement`) for the rest of each session. There is no
retrieval miss, no wrong-file selection, and no evidence instability in this
data. **This evidence does not support any retrieval-architecture change**,
and per this instruction, none was made.

The one real, repeatable quality problem found (4/5 false successes) is a
model patch-correctness issue in the resumable-download edge cases, entirely
downstream of retrieval — retrieval handed the model the right files and
the right acceptance text every time; what the model did with that
information varied.

## What this evidence still does not show

- A statistically strong 64k-vs-128k comparison — n=3 per profile is enough
  to see the false-success pattern is real and repeatable, but not enough
  to attribute the one specification failure to profile width specifically.
- Whether 128k's slightly lower observed median latency (83.3 s vs 92.9 s,
  based on the 2-of-3 that completed) is a real effect or noise — sample
  size is too small either way.
- A hosted-frontier or one-shot comparison (both remain unmeasured — no
  hosted-frontier or one-shot lane run in this comparison).

## Audit locations

- `.apoapsis-eval/priority-a-64k{,-run2,-run3}/` and
  `.apoapsis-eval/priority-a-128k{,-run2,-run3}/` — one comparison report
  and full per-call audit trail (context, measurement, telemetry, patches,
  turns, `held-out-oracle.json`) each.
- `.apoapsis-eval/priority-a-aggregate-full/` — `aggregate.json`/
  `aggregate.md` (`EVAL-AGG-98C10812F0AB`), the 6-attempt aggregate cited
  above.
- The original single-run pass's raw comparison/aggregate JSON remains on
  disk under `.apoapsis-eval/priority-a-aggregate/` (`EVAL-AGG-C4FACF316DC8`,
  never committed); this document supersedes its conclusions with the
  larger 6-attempt sample above rather than deleting the earlier data.

All `.apoapsis-eval/` directories are local, reproducible run output under
the existing gitignore pattern, not committed.
