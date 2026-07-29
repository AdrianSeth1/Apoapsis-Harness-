# ADR 0073 evidence record: request-policy split and web-check evidence

Date: 2026-07-29

| Evidence class | Present here |
| --- | --- |
| Live local inference | Only as the **input** that motivated the change (the preserved Crisis Atlas run). No new live local run was performed. |
| Live hosted inference | None. Not run. |
| Deterministic unit | All test results below. |
| Hand-run CLI | Two constructed products, transcripts below. Real commands, real exit codes, synthetic inputs. |
| Live browser | None. `run_behavioral_probe` still raises. |

## Motivating live-local evidence (pre-existing)

From the preserved Crisis Atlas run (`PLAN-E1B90639E58D`, `qwen3.6-27b` at
32K, 2026-07-29). The approved plan required the dashboard to read and write
a local HTTP API. The configured `web-product-integrity` command passed
`--forbid-external-resources`, which at the time failed on any `fetch` at
all. Both conditions could not be met.

The delivered commit `9b9fccbae2b1502a0aadbb583544360624569202` satisfied the
checks: `app.js` uses in-memory sample data, the dashboard reports
`Offline Mode`, a browser-created incident disappears on reload, and the
backend's incident list stays empty after browser creation. Every configured
check was green.

Separately, after the badge-selector repair, the web check passed while
reporting zero element references cross-checked.

Both observations are live local evidence. Everything below is not.

## Deterministic verification

Environment: Python 3.14.5, pydantic 2.13.4, Windows.

| Command | Result |
| --- | --- |
| `python -m unittest tests.test_verification_contract` | 71 tests, **OK** (25 before this change) |
| `python -m unittest tests.test_verification_contract tests.test_verification tests.test_doctor tests.test_cli tests.test_local_power_session tests.test_agent_loop tests.test_execution_authorization tests.test_ui tests.test_execution_ui` | 319 tests in 323.6s, **OK** (1 expected skip) |
| `python -m compileall -q src tests` | exit 0 |
| `python -m unittest discover -s tests -v` | **not run** |

Exactly one pre-existing test changed behaviour and was rewritten:
`test_a_network_call_is_an_error_for_a_dependency_free_product` asserted that
`fetch('/data')` errors under `--forbid-external-resources`. That assertion
encoded the defect. It is replaced by
`test_a_same_origin_fetch_is_not_an_external_resource`, which asserts the
opposite, and by
`test_the_strict_policy_bans_even_a_same_origin_request`, which preserves the
old behaviour under the new option name.

### Explicitly out of scope

The two pre-existing `test_acceptance_coverage` failures
(`test_stale_worktree_digest_result_does_not_prove_current_code`,
`test_untracked_new_file_creation_invalidates_earlier_proof`) were **not**
addressed by this change and were not run as part of it. They assert on
`_finalize_report`'s return value in `workflow/vertical_slice.py`, which this
change does not touch. They remain in `HANDOFF.md`'s known-limitations
inventory, undiagnosed.

## Hand-run CLI transcripts

### Product 1 — dashboard calling its own backend

`index.html` with `#create`/`#incidents`, `styles.css` styling both, and:

```js
async function load() {
  const r = await fetch('/incidents');
  document.getElementById('incidents').textContent = JSON.stringify(await r.json());
}
document.getElementById('create').addEventListener('click', load);
```

```text
$ apoapsis verify-web-product --root <product> --forbid-external-resources
web product check: 1 document(s), 1 script(s), 1 stylesheet(s), 2 element reference(s) cross-checked
  evidence: 2 element reference(s), 2 CSS selector(s), 2 local asset(s) resolved,
            1 same-origin API reference(s), 0 cross-origin API reference(s),
            0 reference(s) unproven
  ceiling: Static cross-reference only: ... end-to-end browser behavior was NOT
           measured; nothing here executes the product.
PASSED: the product's files agree with each other
exit=0
```

This is the case that could not pass before. The product keeps its
integration and the check is green.

```text
$ apoapsis verify-web-product --root <product> --forbid-runtime-network-apis
...
ERROR app.js [fetch]: the script uses a runtime request API in a product
      declared to make no runtime requests of any kind (--forbid-runtime-network-apis)
      fix: remove the request, or drop --forbid-runtime-network-apis if the
      product is allowed to call its own backend
FAILED: web product integrity check -- 1 error finding(s), 0 warning(s)
exit=1
```

The pre-0073 behaviour, intact, under a name that describes it.

### Product 2 — data-attribute markup, nothing to cross-check

`index.html` containing `<div data-status="open">` and `app.js` containing
only `const STATUSES = ['open','closed'];`.

```text
$ apoapsis verify-web-product --root <product>
web product check: 1 document(s), 1 script(s), 0 stylesheet(s), 0 element reference(s) cross-checked
  evidence: 0 element reference(s), 0 CSS selector(s), 1 local asset(s) resolved,
            0 same-origin API reference(s), 0 cross-origin API reference(s),
            0 reference(s) unproven
  ceiling: This run cross-checked no element references, no CSS selectors, and no
           API references: it establishes only that the files parse and that
           referenced local assets exist. end-to-end browser behavior was NOT
           measured. Do not read this pass as evidence of persistence,
           browser/API integration, or interaction behavior.
WARNING index.html: this check cross-checked nothing ...
PASSED: the product's files agree with each other
exit=0

$ apoapsis verify-web-product --root <product> --treat-warnings-as-errors
... (same evidence and ceiling)
FAILED: web product integrity check -- 0 error finding(s), 1 warning(s)
exit=1
```

This is the shape of the post-badge-repair Crisis Atlas result. It still
passes by default — it is a valid static result — but it now says what it is,
and an owner who wants that to block has `--treat-warnings-as-errors`.

### Classifier spot-check

Run directly against `classify_request_target`:

| Input | Kind |
| --- | --- |
| `/incidents` | `same_origin` |
| `incidents` | `same_origin` |
| `./api/x`, `../api/x` | `same_origin` |
| `/incidents/${id}` | `same_origin` |
| `//cdn.example.com/x` | `cross_origin` |
| `https://cdn.example.com/x` | `cross_origin` |
| `http://localhost:8000/incidents` | `absolute_loopback` |
| `http://127.0.0.1:5000/x` | `absolute_loopback` |
| `http://[::1]:8000/x` | `absolute_loopback` |
| `ws://localhost:9/x`, `wss://example.com/x` | `websocket` |
| `file:///etc/x` | `other_scheme` |
| `${base}/incidents` | `unproven` |
| `` (empty) | `unproven` |

Extraction across one script found all five APIs with their targets:
`fetch`, `new WebSocket`, `new EventSource`, `xhr.open('GET', ...)`, and
`navigator.sendBeacon`; `fetch(endpointVariable)` yielded no literal and
classified as `unproven`.

## What this change does not establish

* It does not repair the Crisis Atlas product. The delivered application
  still serves 404 at `/`, still reports `Offline Mode`, still uses in-memory
  data, and still ships the seed README. This removes the contradiction that
  made deleting the integration the rational move; it does not rebuild the
  integration.
* It does not add the integrated final verification gate (slice C) or the
  operability contract (slice D).
* It does not make discovery or plan validation notice a plan that requires
  an integration while configuring a check forbidding it. That gap is
  recorded in `NEXT_STEPS.md` Priority 2 and folded into slice C.
* It does not execute any product. `verify-web-product` remains a static
  cross-reference, and `--behavior` still raises rather than passing.
* The twelve-point Crisis Atlas regression scenario has not been re-run.
