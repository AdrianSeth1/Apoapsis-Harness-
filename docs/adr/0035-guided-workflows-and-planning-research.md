# ADR 0035: Guided workflows and optional planning research

- Status: Accepted
- Date: 2026-07-20

## Context

The loopback application exposed the implemented task, planning, slice, review,
manual-frontier, and discovery services, but its navigation still assumed that
the operator already understood those internal boundaries. In particular, it
did not plainly answer how to open another repository, when to choose a task
versus a plan, how a slice becomes ready, what happens after a slice completes,
or how to recover a local-model failure with a ChatGPT or Claude subscription.
Research Mode was also available for approved coding tasks but absent from the
discovery-to-planning journey where product and architecture research is most
useful.

## Decision

### One project per application window

`OPEN_APOAPSIS.cmd` accepts an optional explicit project directory and passes it
to the existing `apoapsis ui --project-root` boundary. With no argument it keeps
the previous checkout-local behavior. It validates only prerequisites and
initialization; it never initializes, installs, downloads, or reconfigures
anything. The browser receives no filesystem picker, shell, Git, or project-
creation authority. A new project is an existing Git repository initialized
once with `apoapsis init`, then opened in its own launcher window.

### Organize the UI around user journeys

Home presents three starting points:

1. **Quick change** creates one bounded task.
2. **Plan a larger change** clarifies an idea, optionally researches it, obtains
   a frontier plan, and then exposes one explicitly selected slice at a time.
3. **Needs attention** resumes a stopped task or creates a manual frontier
   repair handoff.

Internal nouns remain visible where they are useful, but do not serve as the
only instructions. Plans and tasks show a small progress sequence. Slice cards
show `Ready` or `Waiting for dependencies` from the same dependency-evidence
function used by packaging, not from browser inference. Technical provenance
and audit detail remain available through progressive disclosure.

Completing a slice does not commit or merge it. The UI says that the operator
must commit and merge the completed slice branch before a dependent slice can
become ready. Apoapsis continues to prove that condition from Git and never
performs it automatically.

### Make manual frontier recovery a first-class continuation

When a task reaches Human Review, the next eligible actions and the manual
ChatGPT/Claude handoff appear before forensic detail. The workflow names the
single generated Markdown file to upload, response import, approval, and apply
steps. Manual subscription use remains measured as `Unmeasured`; Apoapsis does
not automate or scrape subscription websites.

### Add optional research between brief approval and frontier planning

After a discovery `IdeaBrief` is explicitly approved, the operator may choose
Auto, GitHub, Community, or Full research, or skip research. The operation is
durably recorded in the discovery operation ledger and executed by the existing
background worker. It reuses the ADR 0003 Research Engine and its restricted
source adapters. The local research model remains tool-less and receives only
sanitized evidence.

The discovery session records mode, trigger result, compact brief, evidence
IDs, audit directory, and telemetry. Only the compact brief and provenance-
bound evidence IDs enter the immutable frontier planning package. Missing
`[models.local_research]` configuration refuses a research operation before an
operation record is created, while still allowing the user to skip research.
Existing discovery databases are extended additively when opened.

The research run uses a deterministic discovery-scoped `TaskSpecification`
only as typed input to the existing Research Engine. It does not create or
approve a coding task, grant workflow authority, or bypass final plan
validation and user approval.

## Consequences

- A first-time operator can follow the product from repository selection to a
  quick task or a sliced plan without learning the CLI architecture first.
- Planning research is available without adding a second network or model trust
  boundary.
- Browser code continues to render service state and submit typed operations;
  it never owns providers, commands, verification, or completion decisions.
- Native packaging and a system folder picker remain deferred. Supporting them
  later requires a separate decision because both change process ownership and
  capability handling.
- No automatic multi-slice scheduler, commit, merge, subscription automation,
  or silent prerequisite installation is introduced.

## Verification

Deterministic coverage includes the durable fake-engine planning-research path,
fail-closed missing-model behavior, package/Markdown propagation, additive
slice-readiness projection, launcher project selection and initialization
guards, static UI workflow/copy invariants, and the existing fake-provider
discovery and manual-frontier branches. Live browser observations, if any, are
reported separately from deterministic tests in `HANDOFF.md`.
