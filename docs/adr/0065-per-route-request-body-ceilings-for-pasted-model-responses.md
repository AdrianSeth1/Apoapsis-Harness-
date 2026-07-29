# ADR 0065: Per-route request body ceilings for pasted model responses

Date: 2026-07-26

Status: Accepted

Extends ADR 0013 (local operator interface), ADR 0031 (manual
subscription-based frontier handoff), and ADR 0032 (discovery and frontier
planning handoff).

## Context

Importing a real frontier planning response for session `DISC-7D87B2379D8E`
failed with:

```text
request body size is invalid
```

The plan was a legitimate `kind=plan` envelope for a ten-component
architecture: 10 components, 8 integration contracts, 5 architecture
decisions, 5 anticipated hard problems, 17 slices, a delivery contract, and a
verification strategy. Exactly the shape ADR 0032's own planning-quality
requirements demand — the handoff explicitly tells the frontier model that "a
shallow list of coding tasks is not an acceptable plan."

The harness contradicted itself. `discovery.max_response_bytes` defaults to
**2,000,000** and is configurable to 20 MB; `manual_frontier.max_response_bytes`
matches it. Both are documented as the ceiling on a pasted response, checked in
`discovery/manual.py` and `manual_frontier/importer.py` before parsing. But
every UI request body shared one hard-coded transport cap:

```python
_MAX_REQUEST_BYTES = 64 * 1024
```

So the paste routes could never reach their own configured ceiling. The
transport refused roughly thirty times below the size the configuration said
was acceptable, and the domain check that was supposed to enforce the real
policy was unreachable on this path.

The message made it worse. "request body size is invalid" does not say which
size, or what the limit is, or that a limit was even the problem — the same
string was used for a missing `Content-Length`, a zero-length body, and a body
that was too large.

Note what this is *not*. It is not evidence that plans must be split into
smaller pieces for a model to ingest. Slices already exist for exactly that
reason (ADR 0024): the local coder is handed one slice's work brief, never the
whole plan. A large plan document is not a large model input.

## Decision

### Request bodies get per-route ceilings

`_read_json_body(max_bytes=...)` takes an explicit limit. The default stays
`_MAX_REQUEST_BYTES` (64 KB) for ordinary control requests — approvals,
version-checked transitions, short form submissions — which are small by
design and should stay tightly bounded.

The two routes whose body carries a whole pasted model response get a ceiling
derived from the configured domain limit, via
`_pasted_response_body_limit(kind)`:

- `POST /api/discovery/sessions/{id}/import-manual-response` → `discovery`
- `POST /api/reviews/{id}/manual-frontier/import` → `manual_frontier`

The transport ceiling is `configured * 2 + 64 KB`. The pasted response travels
JSON-escaped inside an envelope object, and escaping can in the worst case
double its length; the headroom covers the surrounding fields. Deriving it
from configuration rather than hard-coding a second constant is the point —
the two limits cannot drift apart again.

If configuration is unreadable, the limit falls back to the schema default
(2 MB), not to the control-request cap. A broken config file must not silently
reimpose the bug.

The domain ceilings are unchanged and still authoritative. The transport now
lets a request reach them instead of pre-empting them with a worse message.

### The error names both numbers

```text
request body is 214113 bytes; this endpoint accepts at most 4194304
```

An empty body is reported separately as `request body must not be empty`,
because it is a different mistake.

## Consequences

- A planning response of the size and depth the frontier handoff explicitly
  asks for can now be imported.
- An operator who genuinely exceeds a limit can see by how much, and which
  limit, and can raise `max_response_bytes` in `.apoapsis/config.toml` as an
  informed decision.
- Control routes keep the tight 64 KB bound. Widening the paste routes does
  not widen the surface generally.
- Memory exposure is bounded by configuration rather than by an implicit
  constant. An operator who sets `max_response_bytes` to the 20 MB schema
  maximum is authorizing a ~40 MB transport ceiling on two routes, and that
  follows from a value they set deliberately.
- No change to authority: pasted responses remain operator-declared,
  unverified provenance, still hash-checked against the exported package and
  still schema-validated before anything is applied.

## Alternatives rejected

- **Raise `_MAX_REQUEST_BYTES` globally.** Widens every route, including ones
  that should never accept a large body, to fix two.
- **Chunk the pasted response across several requests.** Real complexity —
  reassembly, partial-upload state, ordering, expiry — to work around a limit
  that was simply set wrong. It would also break the single hash check over
  one complete response that makes the import verifiable.
- **Split the plan itself into multiple smaller handoffs.** This conflates
  document size with model context. The plan is already decomposed into
  slices for execution, and the frontier model produced this plan in one pass
  without difficulty. Splitting the *planning* step would lose exactly what
  ADR 0032 wants from it: one model reconciling the whole architecture,
  cross-slice dependencies, and integration contracts at once.

## Verification performed

```powershell
python -m unittest tests.test_ui tests.test_review_ui tests.test_discovery_ui `
    tests.test_execution_ui tests.test_intake_ui tests.test_manual_frontier_ui `
    tests.test_ui_copy_and_accessibility        # 152/152
python -m compileall -q src tests               # passed
```

New coverage in `tests/test_ui.py`:
`test_oversized_control_request_names_both_sizes` (the control cap still
applies and the message names both numbers) and
`test_a_pasted_frontier_plan_larger_than_the_control_cap_is_accepted` (a
300 KB paste reaches the domain layer and is rejected for a domain reason,
never for size).
