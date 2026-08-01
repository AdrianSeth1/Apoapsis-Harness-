# ADR 0094: Auto-validate frontier plans and show explicit auto-run state

Date: 2026-08-01

## Status

Accepted; implemented in the working tree.

## Context

The guided frontier-planning flow imported a schema-valid plan as `PROPOSED`
and then required the operator to discover and click a separate deterministic
validation action. The Implementation slices page simultaneously labelled an
area as Auto mode but hid the run controls until approval, leaving only muted
copy. This made a controller operation look like an unavailable preference.

## Decision

A successfully imported frontier plan immediately runs the same deterministic
plan validation used by the CLI and UI. The shared operation records the
versioned validation result and audit artifact. A clean plan transitions to
`VALIDATED`; a plan with errors remains `PROPOSED` with exact findings. No
model or project verification command runs during this check.

Human approval remains separate and explicit. Import and validation can never
approve a plan or start a slice.

The Implementation slices page describes automatic execution as a run, not a
persistent toggle. It always shows its current state and next prerequisite:
validate a proposed plan, review and approve a validated plan, or start an
automatic or next-slice run from an approved plan.

## Consequences

- A clean frontier handoff reaches the human decision point without a redundant
  validation click.
- Invalid plans remain inspectable and cannot be approved.
- The same validation implementation and audit behavior serves import, CLI,
  and UI entry points.
- One explicit approval is still required before any execution authorization.
