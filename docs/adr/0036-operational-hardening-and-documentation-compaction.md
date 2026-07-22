# ADR 0036: Operational hardening and documentation compaction

- Status: Accepted
- Date: 2026-07-21

## Context

A preserved planning-to-slice run exposed three independent problems. Discovery
brief generation rejected a source-faithful answer because the prompt added a
Markdown bullet that validation did not recognize. Planning research allowed
the first broad query to consume the entire candidate budget, never searched
later questions, and reported an empty evidence result without saying which
sources produced no findings. Slice execution then spent model calls against a
Python unittest command whose `tests/` directory did not exist while policy
forbade the model from creating tests. Four small accepted edits also exhausted
the default patch budget well before the turn budget.

The canonical `HANDOFF.md` and priority-oriented `NEXT_STEPS.md` had also grown
into duplicated chronological ledgers. Accepted decisions already live in ADRs
and dated live evidence already lives in `docs/evaluation/`; repeating those
narratives in both living documents made current architecture harder to find.

## Decision

### Keep network authority in restricted source adapters

Coding and research models remain tool-less with respect to the network. A
model may propose typed research questions and queries. Apoapsis alone selects
an allowlisted adapter, applies budgets, fetches, sanitizes, quarantines,
attributes, caches, and persists evidence. No model receives a raw browser,
socket, arbitrary URL fetch, shell, or credential access.

Research candidate capacity is distributed across all validated planned
queries so the first query cannot starve the rest. A single actually available
source may fill the fetch budget; source-diversity limits apply only when more
than one source is present. A retrieved source that produces no findings is
recorded explicitly in `rejected-evidence.jsonl`.

The initialized synthesis minimum is one provenance-valid source rather than
three. This avoids treating a single relevant source as equivalent to no
research; provenance remains mandatory and empty evidence still fails closed.

### Canonicalize discovery quotes to the actual user substring

Discovery accepts only narrow presentation differences: a leading list marker,
case, and whitespace. It resolves that candidate against the idea and answers,
then persists the exact matching characters from the user's source. It never
stores the model's normalized spelling as a supposedly verbatim quote and never
accepts a paraphrase.

### Fail before model spend on a known-impossible verification contract

Execution preparation detects the specific contradiction where a required
Python unittest-discovery command points at a missing start directory while
`patch.allow_test_changes` is false. It rejects the operation before creating
an execution record, provider call, or worktree, with actionable remediation.
The loopback HTTP boundary translates that preflight rejection into a structured
conflict response so the browser renders the remediation instead of reporting a
generic fetch failure.
The check is repeated immediately before execution to protect delayed workers.
This is intentionally narrow; Apoapsis does not run verification early or
guess whether an ordinary failing suite is repairable.

Default local/frontier patch-attempt ceilings become 8/5 while turn ceilings
remain 12/8. Patch policy and deterministic verification are unchanged.
The bounded frontier clarification-round default becomes 10; the harness still
enforces that hard maximum and a model cannot extend it.

### Make living documents current-state indexes

`HANDOFF.md` contains the current architecture, authority boundary, component
map, observed snapshot, known limitations, and maintenance triggers.
`NEXT_STEPS.md` contains only active owner and coding-agent priorities. ADRs are
the decision history; dated files in `docs/evaluation/` are the evidence ledger;
the Git history preserves superseded prose. New work updates each fact in one
canonical place and links to it elsewhere instead of copying long narratives.

## Consequences

- Clarification no longer fails because Apoapsis's own bullet formatting was
  copied by the model.
- Research remains safe and auditable while using its bounded internet tools
  more effectively.
- Known-impossible verification setup errors consume zero coding-model calls.
- More of a bounded turn budget can be used for incremental repairs without
  weakening patch validation or increasing the number of model calls.
- Coding agents can load the current system contract without ingesting years of
  duplicated milestone prose.
- General web search, model-owned browsing, automatic verification-command
  selection, and automatic test-policy relaxation remain out of scope.

The test-policy-default statement above was superseded by ADR 0037 after the
owner explicitly requested bounded test authoring for future projects. The
known-impossible preflight remains authoritative when a project explicitly
sets `patch.allow_test_changes = false`.

## Verification

Deterministic coverage includes discovery bullet canonicalization, single-source
research fetch capacity, the existing full research integration path, execution
preflight refusal before operation creation, and generated-config budget values.
Live network and live local-model behavior are not claimed by these tests.

On 2026-07-21, 65 affected deterministic tests passed, along with compileall and
`git diff --check`. A later full-suite run was stopped before completion at the
owner's request; no full-suite result is claimed.
