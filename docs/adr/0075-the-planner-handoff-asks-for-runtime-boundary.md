# ADR 0075: The planner handoff asks for `runtime_boundary`, and enum placeholders show every value

- Status: Accepted
- Date: 2026-07-29

## Context

ADR 0074 added `IntegrationContract.runtime_boundary` and a deterministic
check that rejects a plan whose declared runtime mechanism is forbidden by the
flags of a verification command governing it — the Crisis Atlas contradiction,
made structural.

That check has a precondition nobody was meeting: the planner has to populate
the field. `UNSPECIFIED` is the default and asserts nothing, so a contract left
at the default silently opts out. And nothing in the planning handoff asked for
the field at all.

Two separate reasons a planner would leave it unspecified:

**The prose never mentioned it.** `FRONTIER_PLANNING_QUALITY_REQUIREMENTS`
required "every cross-component integration contract with producer, consumers,
data flow, error behavior, and a concrete verification obligation" — the
pre-0074 field list, verbatim.

**The literal example showed the useless value.** `json_skeleton` rendered an
enum as its first member's value, so the ADR 0066 shape read:

```json
"runtime_boundary": "unspecified"
```

That is not a placeholder a reader replaces. `<string>` obviously demands
substitution; `"unspecified"` reads like an answer. A model copying the shape
faithfully — which is exactly what ADR 0066 asks it to do — produces a contract
with the contradiction check disabled.

This is the same class of defect ADR 0066 itself fixed. There, keys existed
only behind `$ref` indirection and the model invented plausible wrong ones.
Here, the key is present and correct and its *value* quietly defeats a gate.
Structural presence was not sufficient.

## Decision

### 1. Enum placeholders list every permitted value

`json_skeleton` renders an enum as `"<one of: a|b|c>"` rather than the first
member's value:

```json
"runtime_boundary": "<one of: unspecified|in_process|same_origin_http|cross_origin_http|filesystem|subprocess>"
```

The wrapper matches the existing `<string>` convention — obviously a
placeholder, never mistakable for content. It is still derived from the enum
itself, so a member added later cannot be missing from the handoff without
someone remembering to update prose.

`Literal` annotations keep the single-value treatment: a Literal pins a
constant (`schema_version: Literal["1.0"]`), an enum offers alternatives. The
two branches are distinct on purpose.

This affects every enum in the handoff, not only `runtime_boundary`. That is an
improvement in each case: `risk_level`, `kind`, and the rest all previously
showed one arbitrary option where the reader needed the set. For `kind` in
particular the shape now agrees with the surrounding prose, which already
explained both variants.

### 2. The binding requirement names the field

The integration-contract entry in `FRONTIER_PLANNING_QUALITY_REQUIREMENTS`
now requires `runtime_boundary`, enumerates the values, and states the cost of
leaving it unspecified. That list is what the handoff calls "binding", so it is
the right place for an obligation.

### 3. A dedicated handoff section explains why the value matters

Immediately after the literal shape — where a reader is looking at keys — the
handoff now carries `### \`runtime_boundary\` on every integration contract`,
which:

* states that Apoapsis cross-checks the value against governing command flags
  and **rejects the plan** on a contradiction;
* gives the concrete example (`same_origin_http` versus
  `--forbid-runtime-network-apis`);
* explains *why* it matters — such a plan is not merely wrong but
  unsatisfiable, and the only way to make every check pass is to delete the
  integration, which has actually happened; and
* defines each value in one line, including that `unspecified` disables the
  check and is not a way to avoid deciding.

## Consequences

Every generated planning handoff is longer and every enum placeholder changes
shape. No schema, validation rule, or authority boundary changes: this is
entirely about what the handoff asks for and how clearly it asks.

A planner still *may* return `unspecified`, and a contract left there still
produces no finding. That is deliberate — inventing a boundary for a planner
that did not assert one is the prose inference ADR 0074 exists to avoid. What
changes is that the choice is now informed.

**This does not make the contradiction check universal.** It raises the odds a
planner populates the field; it cannot guarantee it. A plan whose contracts are
all `unspecified` still passes validation with the check inert. The remaining
lever, if that turns out to matter in practice, would be requiring a non-default
boundary for contracts whose consumers and producers are in different
components — deliberately not done here, because it would reject valid plans
from planners that simply do not know, and no live evidence yet says the prose
is insufficient.

### Rejected alternatives

**Reorder `RuntimeBoundary` so a meaningful member sorts first.** Would make
the skeleton show e.g. `same_origin_http`, which is worse: it suggests one
specific mechanism rather than asking for a choice. The default must also stay
`UNSPECIFIED` for backward compatibility, so enum order and field default would
disagree.

**Make `runtime_boundary` required with no default.** Breaks every schema-1.1
plan already on disk and rejects plans from planners that genuinely do not
know the mechanism.

**Prose only, leaving the skeleton showing `unspecified`.** The whole finding
of ADR 0066 is that a reader who copies the literal shape does not
cross-reference the prose. Fixing one and not the other repeats that mistake in
the opposite direction.

## Verification

`tests/test_schemas.py::JsonSkeletonTests` — the placeholder listing every
`RuntimeBoundary` member and never equalling `unspecified`; placeholders being
derived from the enum (asserted against `RiskLevel`, so a new member cannot
drift); a true `Literal` still rendering its single value; and the response
`kind` enum showing both variants.

`tests/test_discovery.py` — the generated handoff containing
`"runtime_boundary"` in the literal shape, the dedicated section heading, every
one of the six values documented, the `--forbid-runtime-network-apis` example,
the phrase `rejects the plan`, the warning against using `unspecified` to avoid
deciding, and the binding quality requirement naming the field.

Observed: `tests.test_schemas` 19/19; a six-module run
(`test_discovery test_discovery_ui test_schemas test_research_units
test_provider_and_specification test_intake`) 126/126.
