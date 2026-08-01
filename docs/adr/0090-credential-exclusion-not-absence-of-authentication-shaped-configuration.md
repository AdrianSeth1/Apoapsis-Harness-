# ADR 0090: credential exclusion, not absence of authentication-shaped configuration

Date: 2026-08-01

Status: accepted. Clarifies the containment property Slice 2 recorded and
Slice 7P.3 could not satisfy. Supersedes nothing; the `no-token-environment`
probe is unchanged.

## Context

Slice 2's live gate found a dummy `OPENAI_API_KEY` in the workcell environment
and recorded a correction: "The corrected image supplies no token-like
environment variable." That correction was real, and it was never exercised.
Containment and relay readiness both pass without the Qwen CLI ever starting —
readiness is driven by a controller-side probe against the forwarder — and the
gate stopped before the arms ran. The pinned image kept the variable, and for
six manifest revisions nothing noticed, because Stage 2 had never executed
against a running container at all.

When it finally did, in the v7 rehearsal, `no-token-environment` breached with
exit 0: `OPENAI_API_KEY=apoapsis-local-only` was baked into
`apoapsis-qwen-workcell:0.21.1`.

Rebuilding the image from the corrected Dockerfile removes it. The CLI then
refuses to start:

```
Missing API key for OpenAI-compatible auth. Set settings.security.auth.apiKey,
or set the 'OPENAI_API_KEY' environment variable.
```

Three fresh capability captures against the rebuilt image each produced zero
provider requests, zero tools, and no demonstrated read, write or shell. Qwen
0.21.1 validates that a non-empty key exists before it will talk to anything,
including an unauthenticated local relay.

So two readings of the same probe came apart:

* **no host credential or externally useful secret reaches the workcell** — the
  property the probe was written to enforce;
* **no key-shaped bytes may exist anywhere inside the workcell** — the property
  its implementation happens to express.

The second is unsatisfiable against the pinned CLI. A property nothing can
satisfy protects nothing; it only guarantees the gate is either failed forever
or quietly weakened later, and the second outcome is the one this programme has
learned to expect from itself.

## Decision

**The security property is credential exclusion.** No value that authenticates
anything, grants access to anything outside the workcell, or originates from the
host environment may reach the workcell. Authentication-*shaped* configuration
is permitted when, and only when, it is declared, non-secret, bound by digest,
and demonstrably useless outside the boundary.

**The placeholder lives in the settings file, not the environment.** The
controller writes `security.auth.apiKey = "apoapsis-local-nonsecret-placeholder"`
into `/tmp/qwen-home/settings.json`. The constant is written in the source
(`slot_driver.LOCAL_PLACEHOLDER_API_KEY`), never read from an environment
variable, a file, a secret store or any host state. About that value:

* it is public and non-secret — it appears in this ADR, in the source, and
  verbatim in the evidence;
* it authenticates nothing; the controller-owned relay never inspects it;
* it grants no external access, because `--network none` leaves the loopback
  forwarder as the only reachable endpoint;
* it is accepted solely to satisfy Qwen's OpenAI-compatible client validation;
* it is never sourced from the host environment.

**`no-token-environment` is unchanged.** No token-like environment variable may
exist in the workcell, and the rebuilt image supplies none. The probe is not
narrowed, not exempted, and not given a special case. Moving the value out of
the environment is a change to the system, not to the measurement — which is the
distinction that makes this a fix rather than the evasion it would otherwise be.

**A separate check binds what the placeholder may be.** `no-token-environment`
answers "is there a token-like variable in the environment"; it cannot answer
"is the one required key the declared one". `observe_workcell_configuration`
reads the settings the CLI will actually use, from inside the container, and
requires all of:

* `security.auth.apiKey` is exactly the declared placeholder;
* the provider `baseUrl` is the bound loopback forwarder endpoint;
* no other API keys, tokens or credential files exist — checked across the
  environment, the settings tree and a list of credential paths;
* the container's network is `none`, read from inside rather than from the flag
  the controller passed;
* the relay's upstream and route policy are the bound ones;
* the settings bytes digest to the value the manifest binds.

The last one is what makes the rest enforceable. The settings digest is a bound
controlled variable: changing the placeholder, the endpoint or any other
setting changes the digest, which invalidates comparability and forces a new
manifest rather than a quiet edit.

**The evidence records the value, and classifies it.** The scripted provider
captures the `Authorization` header verbatim and labels it
`declared_placeholder`, `unrecognised` or `absent`. Redaction was rejected: the
only interesting question is whether the arm sent the declared placeholder or
something else, and a redacted log cannot distinguish them. A value this
evidence has no account of is the one shape that could be a real credential, and
`unrecognised` is how it becomes visible.

## Consequences

The rebuilt workcell image carries no token-like environment variable and the
CLI runs. `no-token-environment` passes for the reason it was written for
rather than by exemption.

The placeholder is now a controlled variable with the same status as the model
digest or the server argv: bound, checked from inside the box, and impossible to
change without a new manifest. That is stricter than the situation this replaces
— the old image's key was inside the image, unbound, and unmeasured.

This ADR does not license further authentication-shaped configuration. Anything
new of this kind requires its own decision, its own declaration, and its own
place in the configuration check; the default remains that nothing
credential-shaped is permitted.
