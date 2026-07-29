# ADR 0073: Same-origin requests are not external resources, and a web check reports its own evidence

- Status: Accepted
- Date: 2026-07-29

## Context

Two defects in `apoapsis.verification.web_product`, both visible in the
preserved Crisis Atlas run (`PLAN-E1B90639E58D`, 2026-07-29).

### Defect one: one option was doing two jobs

`verify_web_product(..., forbid_external_resources=True)` treated *every*
detected request API as forbidden:

```python
_NETWORK_CALL = re.compile(r"\b(fetch|XMLHttpRequest|WebSocket|EventSource)\b")
...
if forbid_external_resources and refs.network_calls:
    findings.append(WebProductFinding(code=NETWORK_CALL, severity=ERROR, ...))
```

The URL was never examined. The flag's own help text said "fail on any
external URL, CDN reference, or network API use", which collapses two
policies that are not the same policy:

* **no third-party or internet dependency** — the product must not need a
  CDN, an analytics host, or any origin it does not serve; and
* **no browser communication with the product's own backend** — the product
  must make no runtime request at all.

The first is a common, sensible requirement for a dependency-free browser
product. The second is a much stronger and much rarer one. Collapsing them
means a product forbidden to depend on a CDN is equally forbidden to talk to
its own API.

Crisis Atlas required exactly that integration. Its plan specified a
dashboard backed by a local HTTP API, and its configured
`web-product-integrity` command passed `--forbid-external-resources`. So the
plan required `fetch('/incidents')` and the plan's own verification contract
made `fetch('/incidents')` an error. There was no way to satisfy both.

The implementation resolved the contradiction the only way it could: it made
the check green by removing the integration. The delivered `app.js` uses
in-memory sample data, the dashboard reports `Offline Mode`, a
browser-created incident vanishes on reload, and the backend's incident list
stays empty. Every configured check passed.

This is not a model failure. Given a contract that forbids the mechanism the
objective requires, deleting the mechanism is the *only* action that
satisfies the checks. The harness asked for something impossible and then
accepted the degenerate answer.

### Defect two: a passing check did not say how little it had checked

Late in the same run, a badge-selector repair replaced computed class names
with data attributes. The web check then passed having cross-checked **zero**
element references — there were no longer any `getElementById`/
`querySelector` calls for it to resolve.

That is a *valid* static result. It is also nearly worthless, and nothing in
the report distinguished it from a run that resolved forty references across
three files. The CLI printed one line — "N element reference(s)
cross-checked" — and then `PASSED`. An owner scanning output, or a model
reading captured output, sees a green check either way.

ADR 0069 already established the principle: evidence strength is itself a
fact the harness can compute and must attach to the outcome. It applied that
to *contract configuration*. It did not apply it to what an individual check
run actually examined.

## Decision

### 1. Classify request targets, and split the policy in two

Add `classify_request_target(url) -> RequestTargetKind`, the single
definition of "external" for both script requests and document assets:

| URL form | Kind | Same-origin? |
| --- | --- | --- |
| `/incidents`, `/incidents/${id}` | `SAME_ORIGIN` | yes |
| `incidents`, `./api/x`, `../api/x` | `SAME_ORIGIN` | yes |
| `//cdn.example.com/x` | `CROSS_ORIGIN` | no |
| `https://cdn.example.com/x` | `CROSS_ORIGIN` | no |
| `http://localhost:8000/x`, `http://127.0.0.1:5000/x`, `http://[::1]:8000/x` | `ABSOLUTE_LOOPBACK` | no |
| `ws://…`, `wss://…` | `WEBSOCKET` | no |
| `file:…` and other schemes | `OTHER_SCHEME` | no |
| `${base}/incidents`, non-literal argument | `UNPROVEN` | unknown |

Two rulings worth stating explicitly:

**Absolute loopback is not same-origin.** `http://localhost:8000/incidents`
is local today and still a hard-coded origin: it breaks the moment the
product is served on another port or host, and it is a different origin from
the page under any normal deployment. It is grouped with cross-origin for
policy and given its own kind so the remediation can say the useful thing —
use a root-relative path.

**A leading `/` settles the origin before interpolation can affect it.**
`/incidents/${id}` is same-origin. `${base}/incidents` is `UNPROVEN`, because
`base` could be a third-party host and this module cannot know.

The policies then become:

`forbid_external_resources` (existing name, **narrowed**)
: No third-party origin. Cross-origin, protocol-relative, WebSocket,
  absolute-loopback, and other-scheme targets are errors, as are external
  `<script src>`/`<link href>` assets. Same-origin requests are **not**
  errors. `UNPROVEN` targets are warnings — the check cannot show they stay
  on the product's origin, so it must not report them as compliant, but it
  has no evidence of a violation either.

`forbid_runtime_network_apis` (new, off by default)
: No runtime request of any kind, same-origin included. This is exactly the
  pre-0073 blanket behaviour, preserved for owners who genuinely mean it,
  under a name that says what it does.

The two are independent and may be combined. `--forbid-runtime-network-apis`
is the migration path for anyone relying on the old meaning.

### 2. Report requests as evidence, not only as violations

Every runtime request now produces a finding, including compliant ones, at
INFO severity:

* `SAME_ORIGIN_REQUEST` — INFO always. An owner reading the report can see
  that the product does call its backend, and at which paths, rather than
  inferring it from silence.
* `CROSS_ORIGIN_REQUEST` — ERROR under `forbid_external_resources`, INFO
  otherwise, carrying `target_kind` so the wording can distinguish a CDN
  from a hard-coded loopback URL.
* `UNPROVEN_REQUEST_TARGET` — WARNING under `forbid_external_resources`,
  INFO otherwise.
* `NETWORK_CALL` — retained, narrowed: emitted only under
  `forbid_runtime_network_apis`, meaning "a request API is used at all".

`WebProductFinding` gains an optional `target_kind`. Findings about markup
and styles, which have no target, are unchanged.

### 3. Count the evidence and state the ceiling

`WebProductReport` gains `evidence: WebCheckEvidence`:

* `element_references_checked`
* `css_selectors_checked`
* `local_assets_resolved`
* `same_origin_api_references`
* `cross_origin_api_references`
* `dynamic_references_unproven` (computed request targets plus selectors too
  complex to analyze — the same category of "looked at it, proved nothing")
* `end_to_end_behavior_measured` — hard-coded `False`, not a parameter, so
  no caller can set it without a real probe existing. `run_behavioral_probe`
  still raises.

`ceiling_statement()` renders one sentence naming what the result can and
cannot support. `is_negligible` is true when nothing was cross-checked, and
raises a `NEGLIGIBLE_EVIDENCE` warning naming the situation. The CLI prints
the counts and the ceiling on **every** run, pass or fail.

`--treat-warnings-as-errors` therefore now turns "this check proved nothing"
into a failure, for owners who want that.

### 4. Ask about criteria a static check cannot prove

`verification/contract.py` gains `CRITERION_ASKS_FOR_BEHAVIOR`: a WARNING
raised when an owner's own criterion text mentions persistence, surviving a
reload or restart, a round trip through an API, browser/API integration, or
interaction behavior.

The word table is explicit and readable (`_BEHAVIORAL_CRITERION_WORDS`) so
an owner who disagrees with a finding can see exactly why it fired. It is
deliberately conservative — "renders" and "displays" are absent, because a
static check can reasonably speak to markup.

This is distinct from the heuristic that module still refuses: it does not
inspect a command's `argv` to guess what the command does. It reads the
owner's own words back to them and asks a question. It never changes
`evidence_level`, never blocks, and will produce false positives. The cost of
a false positive is one line an owner dismisses; the cost of the false
negative it prevents was a Crisis Atlas criterion about surviving a browser
reload, mapped to a static file check, reported as proven.

## Consequences

### Migration

**`--forbid-external-resources` no longer fails on same-origin requests.** A
configured `web-product-integrity` command that relied on it as a blanket ban
must add `--forbid-runtime-network-apis` to keep the old behaviour. No
configuration is rewritten automatically; the flag keeps its name because its
name always described the narrower policy, and it is the implementation that
was wrong.

Owners who *wanted* the narrow policy and worked around the old behaviour —
by omitting the flag entirely, and so also losing CDN detection — can now
enable it and get what they asked for.

The `apoapsis init` configuration comment now states the distinction.

### Behaviour changes an operator will see

* A product that calls its own backend passes `--forbid-external-resources`.
* Compliant requests appear in the report as INFO findings; output is longer
  and says more.
* Every run prints evidence counts and a ceiling sentence.
* A check that cross-referenced nothing raises a warning, and fails under
  `--treat-warnings-as-errors`.
* Doctor and the report surface a new WARNING for criteria that describe
  runtime behavior.

### What this does not do

It does not make `verify-web-product` a browser. It still executes nothing,
and `run_behavioral_probe` still raises rather than degrading a requested
behavioral check into a silent pass. The check is deterministic,
dependency-free, and offline, as before.

It does not repair Crisis Atlas. The delivered product still serves 404 at
`/`, still says `Offline Mode`, and still ships the seed README. This removes
the contradiction that made deleting the integration the rational move; it
does not add the integrated final gate (slice C) or the operability contract
(slice D), and it does not re-run the regression scenario.

It does not touch the authority boundary. No model chooses, proposes, or
influences a policy, a classification, or a finding.

### Rejected alternatives

**Allow absolute loopback URLs as same-origin.** Tempting, because the
owner's intent is obviously local. But a hard-coded `http://localhost:8000`
is a real portability defect, and calling it same-origin would mean the check
stays silent about it. A distinct kind with pointed remediation says more.

**Treat `UNPROVEN` targets as errors under `forbid_external_resources`.**
Fails working products whose API base is legitimately configured at runtime.
A warning that is counted as unproven, and that `--treat-warnings-as-errors`
escalates, reports the uncertainty without inventing a violation.

**Keep one option and add an `allow_same_origin=True` default.** The default
would silently change the meaning of every existing configuration with no
name change to notice. A separately named option makes the stricter policy an
explicit, visible choice, which is what the remediation handoff asked for.

**Infer from a command's `argv` whether it exercises the product.**
Explicitly refused by ADR 0069 and still refused. `CRITERION_ASKS_FOR_BEHAVIOR`
reads criterion text, not command intent.

## Verification

`tests/test_verification_contract.py` grew from 25 to 71 cases:

* `RequestTargetClassificationTests` — every URL form in the table above,
  including all four loopback spellings and the interpolation boundary.
* `ScriptRequestExtractionTests` — `fetch`, `new WebSocket`,
  `new EventSource`, `xhr.open(method, url)`, and `navigator.sendBeacon`
  each found with their target; a computed argument yielding no literal.
* `WebProductRequestPolicyTests` — `fetch('/incidents')` passing
  `--forbid-external-resources` (the Crisis Atlas case, as a test); third-party
  HTTPS, protocol-relative, WebSocket, and absolute-loopback URLs each still
  rejected with the right `target_kind`; same-origin XHR and bare relative
  `fetch('incidents')` allowed; external stylesheet assets still rejected; the
  strict policy banning a same-origin request; the two policies producing
  independent findings; and no policy reporting both as information.
* `WebCheckEvidenceReportingTests` — counts on a real product, the
  zero-evidence pass naming itself and failing under
  `--treat-warnings-as-errors`, and unanalyzed selectors counted as unproven.
* `BehavioralCriterionFindingTests` — the word table, a statically provable
  criterion not flagged, and the finding being a WARNING that leaves
  `evidence_level` at `CRITERION_MAPPED`.

The `verify-web-product` CLI was additionally run by hand against two
constructed products; see the dated evaluation record for the exact output.
