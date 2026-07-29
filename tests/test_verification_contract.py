"""Coverage for ADR 0069's two additions: knowing how weak a verification
contract is, and having something stronger to configure instead.

The organizing question here is the one the Focus Orbit trial answered
badly: given source files that individually contain everything a checklist
asked for, does anything notice that they do not describe the same
application?
"""

from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
from pathlib import Path

from apoapsis.config import CompletionPolicy
from apoapsis.specification.schema import AcceptanceCriterion, SourceKind
from apoapsis.verification.contract import (
    ContractEvidenceLevel,
    ContractFindingCode,
    ContractFindingSeverity,
    assess_verification_contract,
    behavioral_concerns,
)
from apoapsis.verification.runner import VerificationCommand
from apoapsis.verification.web_product import (
    BrowserProbeUnavailableError,
    RequestTargetKind,
    WebProductFindingCode,
    WebProductFindingSeverity,
    analyze_script,
    classify_request_target,
    css_rule_selectors,
    run_behavioral_probe,
    selector_targets,
    verify_web_product,
)
from tests.helpers import make_specification


def command(name: str, **overrides: object) -> VerificationCommand:
    values: dict[str, object] = {
        "name": name,
        "category": "tests",
        "argv": [sys.executable, "-c", "raise SystemExit(0)"],
    }
    values.update(overrides)
    return VerificationCommand(**values)


class ContractAssessmentTests(unittest.TestCase):
    def specification_with_criteria(self, *methods: str | None):
        specification = make_specification()
        criteria = [
            AcceptanceCriterion(
                id=f"AC-{index + 1}",
                text=f"criterion {index + 1}",
                source=SourceKind.USER,
                source_reference="message-1",
                verification_method=method,
            )
            for index, method in enumerate(methods)
        ]
        return specification.model_copy(update={"acceptance_criteria": criteria})

    def test_an_empty_contract_can_prove_nothing(self) -> None:
        assessment = assess_verification_contract(None, [])
        self.assertEqual(assessment.evidence_level, ContractEvidenceLevel.NONE)
        self.assertFalse(assessment.proves_configured_criteria)
        self.assertEqual(
            [item.code for item in assessment.findings],
            [ContractFindingCode.NO_VERIFICATION_COMMAND],
        )

    def test_a_contract_with_nothing_required_can_prove_nothing(self) -> None:
        assessment = assess_verification_contract(None, [command("unit", required=False)])
        self.assertEqual(assessment.evidence_level, ContractEvidenceLevel.NONE)
        codes = [item.code for item in assessment.findings]
        self.assertIn(ContractFindingCode.NO_REQUIRED_COMMAND, codes)

    def test_the_live_trial_contract_grades_as_development_only(self) -> None:
        """The exact shape TASK-33E0EB6476C4 ran under: one required
        command, no acceptance designation, seven unmapped criteria."""

        specification = self.specification_with_criteria(*([None] * 7))
        assessment = assess_verification_contract(
            specification, [command("unit-tests")], CompletionPolicy.BASELINE
        )
        self.assertEqual(
            assessment.evidence_level, ContractEvidenceLevel.DEVELOPMENT_ONLY
        )
        self.assertFalse(assessment.proves_configured_criteria)
        self.assertEqual(assessment.criteria_mapped_to_acceptance_command, 0)
        codes = [item.code for item in assessment.findings]
        self.assertIn(ContractFindingCode.NO_ACCEPTANCE_COMMAND, codes)
        self.assertIn(ContractFindingCode.COMPLETION_POLICY_IGNORES_CRITERIA, codes)
        self.assertEqual(
            codes.count(ContractFindingCode.UNMAPPED_ACCEPTANCE_CRITERION), 7
        )
        self.assertTrue(assessment.critical_findings)

    def test_a_criterion_mapped_to_a_non_acceptance_command_is_not_proof(self) -> None:
        specification = self.specification_with_criteria("unit-tests")
        assessment = assess_verification_contract(
            specification,
            [command("unit-tests"), command("browser", acceptance=True, required=False)],
        )
        self.assertEqual(
            assessment.evidence_level, ContractEvidenceLevel.ACCEPTANCE_DESIGNATED
        )
        self.assertIn(
            ContractFindingCode.CRITERION_MAPPED_TO_NON_ACCEPTANCE_COMMAND,
            [item.code for item in assessment.findings],
        )

    def test_a_criterion_naming_an_unconfigured_command_is_reported(self) -> None:
        specification = self.specification_with_criteria("does-not-exist")
        assessment = assess_verification_contract(
            specification, [command("unit-tests", acceptance=True)]
        )
        self.assertIn(
            ContractFindingCode.CRITERION_MAPPED_TO_UNKNOWN_COMMAND,
            [item.code for item in assessment.findings],
        )

    def test_a_fully_mapped_contract_reaches_the_strongest_level(self) -> None:
        specification = self.specification_with_criteria("browser", "browser")
        assessment = assess_verification_contract(
            specification,
            [command("browser", acceptance=True)],
            CompletionPolicy.STRICT,
        )
        self.assertEqual(assessment.evidence_level, ContractEvidenceLevel.CRITERION_MAPPED)
        self.assertTrue(assessment.proves_configured_criteria)
        self.assertEqual(assessment.criteria_mapped_to_acceptance_command, 2)
        self.assertEqual(
            [item.severity for item in assessment.findings],
            [ContractFindingSeverity.INFO],
        )

    def test_the_assessment_never_guesses_what_a_command_does(self) -> None:
        """Two commands with identical structure and wildly different argv
        must grade identically: any difference would mean the module had
        started inferring behavior from a name it cannot execute."""

        grep = assess_verification_contract(
            None, [command("check", argv=["grep", "-q", "timer", "app.js"])]
        )
        real = assess_verification_contract(
            None, [command("check", argv=["node", "test/behavior.js"])]
        )
        self.assertEqual(grep.evidence_level, real.evidence_level)
        self.assertEqual(
            [item.code for item in grep.findings],
            [item.code for item in real.findings],
        )


class WebProductAnalysisUnitTests(unittest.TestCase):
    def test_script_element_references_are_collected(self) -> None:
        refs = analyze_script(
            """
            const a = document.getElementById('start');
            const b = document.querySelector('#reset');
            const c = document.querySelectorAll('.mode-button');
            const d = document.getElementsByClassName('ring');
            """
        )
        self.assertEqual(refs.ids, {"start", "reset"})
        self.assertEqual(refs.classes, {"mode-button", "ring"})

    def test_a_selector_too_complex_to_analyze_is_counted_not_guessed(self) -> None:
        refs = analyze_script("document.querySelector('input[type=\"radio\"]:checked');")
        self.assertEqual(refs.unanalyzed, 1)
        self.assertEqual(refs.ids, set())
        self.assertEqual(refs.classes, set())

    def test_runtime_created_markup_counts_as_provided(self) -> None:
        refs = analyze_script(
            'root.innerHTML = `<div id="panel" class="card ${tone(x)} wide"></div>`;'
        )
        self.assertIn("panel", refs.dynamic_ids)
        self.assertIn("card", refs.dynamic_classes)
        self.assertIn("wide", refs.dynamic_classes)
        # The interpolation is not a class name and must not be recorded.
        self.assertNotIn("${tone(x)}", refs.dynamic_classes)

    def test_duplicate_top_level_functions_are_detected(self) -> None:
        refs = analyze_script("function view() {}\nfunction view() {}\n")
        self.assertEqual(refs.duplicate_functions, ["view"])

    def test_css_rule_selectors_skip_at_rules_but_keep_nested_rules(self) -> None:
        selectors = css_rule_selectors(
            """
            /* comment */
            .card { color: red }
            @media (prefers-reduced-motion: reduce) { .ring { animation: none } }
            @keyframes spin { from { opacity: 0 } }
            """
        )
        self.assertIn(".card", selectors)
        self.assertIn(".ring", selectors)
        self.assertNotIn("@keyframes spin", selectors)

    def test_selector_targets_ignore_pseudo_classes(self) -> None:
        ids, classes = selector_targets(".btn:focus-visible #panel .a.b")
        self.assertEqual(ids, {"panel"})
        self.assertEqual(classes, {"btn", "a", "b"})


class WebProductCrossReferenceTests(unittest.TestCase):
    """The regression this whole ADR exists for, reproduced from the live
    Focus Orbit failure and reduced to its essentials."""

    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="apoapsis-web-product-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)

    def write(self, name: str, content: str) -> None:
        (self.root / name).write_text(content, encoding="utf-8")

    def write_working_product(self) -> None:
        self.write(
            "index.html",
            """<!doctype html><html><head><link rel="stylesheet" href="styles.css">
            </head><body>
            <button id="start" class="btn">Start</button>
            <div id="timer" class="ring"></div>
            <script src="app.js"></script></body></html>""",
        )
        self.write("styles.css", ".btn { color: red } .ring { color: blue }")
        self.write(
            "app.js",
            "document.getElementById('start').addEventListener('click', () => {"
            "document.getElementById('timer').textContent = '25:00'; });",
        )

    def test_a_consistent_product_passes(self) -> None:
        self.write_working_product()
        report = verify_web_product(self.root)
        self.assertTrue(report.passed(), msg=[item.detail for item in report.findings])
        self.assertEqual(report.errors, [])
        self.assertGreater(report.checked_references, 0)

    def test_the_focus_orbit_failure_is_caught(self) -> None:
        """Every fragment a static checklist would look for is present, and
        none of them refer to the same elements."""

        self.write(
            "index.html",
            """<!doctype html><html><head><link rel="stylesheet" href="styles.css">
            </head><body>
            <button id="focus-mode" class="mode">Focus</button>
            <svg class="orbit"><circle class="orbit-line"></circle></svg>
            <script src="app.js"></script></body></html>""",
        )
        self.write(
            "styles.css",
            ".progress-ring { stroke: white } .btn-primary { color: red }",
        )
        self.write(
            "app.js",
            "document.getElementById('mode-focus').addEventListener('click', run);\n"
            "document.getElementById('status-live').textContent = 'ready';\n",
        )
        report = verify_web_product(self.root)
        self.assertFalse(report.passed())
        unresolved = {
            item.symbol
            for item in report.errors
            if item.code == WebProductFindingCode.UNRESOLVED_ELEMENT_ID
        }
        self.assertEqual(unresolved, {"mode-focus", "status-live"})
        dead = {
            item.symbol
            for item in report.findings
            if item.code == WebProductFindingCode.DEAD_STYLE_RULE
        }
        self.assertEqual(dead, {".progress-ring", ".btn-primary"})

    def test_an_element_the_script_creates_is_not_reported_missing(self) -> None:
        self.write(
            "index.html",
            '<!doctype html><html><body><div id="root"></div>'
            '<script src="app.js"></script></body></html>',
        )
        self.write(
            "app.js",
            'document.getElementById("root").innerHTML = `<b id="clock"></b>`;\n'
            'document.getElementById("clock").textContent = "0";\n',
        )
        report = verify_web_product(self.root)
        self.assertEqual(report.errors, [])

    def test_an_explicitly_optional_element_is_accepted(self) -> None:
        self.write(
            "index.html",
            '<!doctype html><html><body><script src="app.js"></script></body></html>',
        )
        self.write("app.js", "document.getElementById('late').focus();")
        self.assertFalse(verify_web_product(self.root).passed())
        allowed = verify_web_product(self.root, optional_elements={"late"})
        self.assertTrue(allowed.passed())

    def test_a_duplicate_id_is_an_error(self) -> None:
        self.write(
            "index.html",
            '<!doctype html><html><body><p id="x"></p><p id="x"></p></body></html>',
        )
        report = verify_web_product(self.root)
        self.assertIn(
            WebProductFindingCode.DUPLICATE_ELEMENT_ID,
            [item.code for item in report.errors],
        )

    def test_a_missing_local_asset_is_an_error(self) -> None:
        self.write(
            "index.html",
            '<!doctype html><html><body><script src="missing.js"></script></body></html>',
        )
        report = verify_web_product(self.root)
        self.assertIn(
            WebProductFindingCode.MISSING_LOCAL_ASSET,
            [item.code for item in report.errors],
        )

    def test_a_root_absolute_reference_resolves_against_the_product_root(self) -> None:
        self.write(
            "index.html",
            '<!doctype html><html><body><script src="/app.js"></script></body></html>',
        )
        self.write("app.js", "const x = 1;")
        self.assertEqual(verify_web_product(self.root).errors, [])

    def test_an_external_resource_is_information_until_it_is_forbidden(self) -> None:
        self.write(
            "index.html",
            '<!doctype html><html><body>'
            '<script src="https://cdn.example.com/x.js"></script></body></html>',
        )
        relaxed = verify_web_product(self.root)
        self.assertTrue(relaxed.passed())
        self.assertIn(
            WebProductFindingCode.EXTERNAL_RESOURCE_REFERENCE,
            [item.code for item in relaxed.findings],
        )
        strict = verify_web_product(self.root, forbid_external_resources=True)
        self.assertFalse(strict.passed())

    def test_a_same_origin_fetch_is_not_an_external_resource(self) -> None:
        """The Crisis Atlas defect, as a test (ADR 0073).

        Before the policy split this exact product failed
        ``--forbid-external-resources``, so a plan that *required* the
        dashboard to call its own backend could only go green by deleting
        the integration. It did.
        """

        self.write(
            "index.html",
            '<!doctype html><html><body><script src="app.js"></script></body></html>',
        )
        self.write("app.js", "fetch('/incidents').then(r => r.json());")
        report = verify_web_product(self.root, forbid_external_resources=True)
        self.assertTrue(report.passed(), msg=[item.detail for item in report.findings])
        self.assertEqual(report.errors, [])
        same_origin = [
            item
            for item in report.findings
            if item.code == WebProductFindingCode.SAME_ORIGIN_REQUEST
        ]
        self.assertEqual([item.symbol for item in same_origin], ["/incidents"])
        self.assertEqual(report.evidence.same_origin_api_references, 1)
        self.assertEqual(report.evidence.cross_origin_api_references, 0)

    def test_a_missing_entry_document_stops_the_check(self) -> None:
        report = verify_web_product(self.root)
        self.assertFalse(report.passed())
        self.assertEqual(
            [item.code for item in report.findings],
            [WebProductFindingCode.NO_ENTRY_DOCUMENT],
        )

    def test_warnings_do_not_fail_unless_the_owner_asks(self) -> None:
        self.write_working_product()
        self.write("styles.css", ".btn { color: red } .ring {} .nowhere { color: red }")
        report = verify_web_product(self.root)
        self.assertTrue(report.passed())
        self.assertFalse(report.passed(treat_warnings_as_errors=True))
        self.assertEqual(
            [item.severity for item in report.warnings],
            [WebProductFindingSeverity.WARNING],
        )


class RequestTargetClassificationTests(unittest.TestCase):
    """Every URL form the two policies have to tell apart (ADR 0073).

    Table-driven on purpose: the whole point of the ADR is that a single
    rule was doing two jobs, and the cheapest way to keep them separate is
    an explicit list of what each form is.
    """

    def test_same_origin_forms(self) -> None:
        for url in ("/incidents", "incidents", "./api/x", "../api/x", "/incidents/${id}"):
            with self.subTest(url=url):
                self.assertEqual(
                    classify_request_target(url), RequestTargetKind.SAME_ORIGIN
                )

    def test_cross_origin_forms(self) -> None:
        for url in (
            "https://cdn.example.com/x.js",
            "http://example.com/api",
            "//cdn.example.com/x.js",
        ):
            with self.subTest(url=url):
                self.assertEqual(
                    classify_request_target(url), RequestTargetKind.CROSS_ORIGIN
                )

    def test_absolute_loopback_is_its_own_kind(self) -> None:
        """Local today, still a hard-coded origin. Grouped with cross-origin
        for policy, named separately so the remediation can say the useful
        thing: use a root-relative path instead."""

        for url in (
            "http://localhost:8000/incidents",
            "http://127.0.0.1:5000/x",
            "https://0.0.0.0:9/x",
            "http://[::1]:8000/x",
        ):
            with self.subTest(url=url):
                self.assertEqual(
                    classify_request_target(url), RequestTargetKind.ABSOLUTE_LOOPBACK
                )

    def test_websocket_and_other_schemes(self) -> None:
        self.assertEqual(
            classify_request_target("wss://example.com/live"),
            RequestTargetKind.WEBSOCKET,
        )
        self.assertEqual(
            classify_request_target("ws://localhost:9/live"),
            RequestTargetKind.WEBSOCKET,
        )
        self.assertEqual(
            classify_request_target("file:///etc/passwd"),
            RequestTargetKind.OTHER_SCHEME,
        )

    def test_interpolation_before_the_origin_is_unproven(self) -> None:
        """`${base}/incidents` could be anything. `/incidents/${id}` cannot:
        the leading slash settled the origin before the interpolation."""

        self.assertEqual(
            classify_request_target("${base}/incidents"), RequestTargetKind.UNPROVEN
        )
        self.assertEqual(
            classify_request_target("/incidents/${id}"), RequestTargetKind.SAME_ORIGIN
        )
        self.assertEqual(classify_request_target(""), RequestTargetKind.UNPROVEN)


class ScriptRequestExtractionTests(unittest.TestCase):
    def test_every_request_api_is_found_with_its_target(self) -> None:
        refs = analyze_script(
            "fetch('/incidents');\n"
            "const s = new WebSocket('wss://example.com/live');\n"
            "const e = new EventSource('/stream');\n"
            "const xhr = new XMLHttpRequest();\n"
            "xhr.open('GET', '/incidents');\n"
            "navigator.sendBeacon('/telemetry', payload);\n"
        )
        found = {(item.api, item.target, item.kind) for item in refs.requests}
        self.assertEqual(
            found,
            {
                ("fetch", "/incidents", RequestTargetKind.SAME_ORIGIN),
                ("WebSocket", "wss://example.com/live", RequestTargetKind.WEBSOCKET),
                ("EventSource", "/stream", RequestTargetKind.SAME_ORIGIN),
                (
                    "XMLHttpRequest.open",
                    "/incidents",
                    RequestTargetKind.SAME_ORIGIN,
                ),
                (
                    "navigator.sendBeacon",
                    "/telemetry",
                    RequestTargetKind.SAME_ORIGIN,
                ),
            },
        )
        self.assertEqual(
            refs.network_apis,
            ["EventSource", "WebSocket", "XMLHttpRequest", "fetch", "sendBeacon"],
        )

    def test_a_computed_target_has_no_literal_and_is_unproven(self) -> None:
        refs = analyze_script("fetch(endpointFor(kind));")
        self.assertEqual(len(refs.requests), 1)
        self.assertIsNone(refs.requests[0].target)
        self.assertEqual(refs.requests[0].kind, RequestTargetKind.UNPROVEN)


class WebProductRequestPolicyTests(WebProductCrossReferenceTests):
    """The two policies act independently and mean different things."""

    def _product(self, script: str, *, markup: str = "") -> None:
        self.write(
            "index.html",
            "<!doctype html><html><body>"
            + markup
            + '<script src="app.js"></script></body></html>',
        )
        self.write("app.js", script)

    def test_a_third_party_url_is_still_rejected(self) -> None:
        self._product("fetch('https://api.example.com/incidents');")
        report = verify_web_product(self.root, forbid_external_resources=True)
        self.assertFalse(report.passed())
        self.assertEqual(
            [item.code for item in report.errors],
            [WebProductFindingCode.CROSS_ORIGIN_REQUEST],
        )
        self.assertEqual(report.errors[0].target_kind, RequestTargetKind.CROSS_ORIGIN)

    def test_a_protocol_relative_url_is_rejected(self) -> None:
        self._product("fetch('//cdn.example.com/incidents');")
        report = verify_web_product(self.root, forbid_external_resources=True)
        self.assertFalse(report.passed())
        self.assertEqual(report.errors[0].target_kind, RequestTargetKind.CROSS_ORIGIN)

    def test_a_websocket_to_an_absolute_origin_is_rejected(self) -> None:
        self._product("const s = new WebSocket('wss://example.com/live');")
        report = verify_web_product(self.root, forbid_external_resources=True)
        self.assertFalse(report.passed())
        self.assertEqual(report.errors[0].target_kind, RequestTargetKind.WEBSOCKET)

    def test_an_absolute_loopback_url_is_rejected_with_its_own_wording(self) -> None:
        self._product("fetch('http://localhost:8000/incidents');")
        report = verify_web_product(self.root, forbid_external_resources=True)
        self.assertFalse(report.passed())
        finding = report.errors[0]
        self.assertEqual(finding.target_kind, RequestTargetKind.ABSOLUTE_LOOPBACK)
        self.assertIn("hard-coded origin", finding.detail)
        self.assertIn("root-relative", finding.remediation)

    def test_a_same_origin_xhr_is_allowed(self) -> None:
        self._product(
            "const xhr = new XMLHttpRequest();\nxhr.open('POST', '/incidents');\n"
        )
        report = verify_web_product(self.root, forbid_external_resources=True)
        self.assertEqual(report.errors, [])
        self.assertEqual(report.evidence.same_origin_api_references, 1)

    def test_a_relative_fetch_without_a_leading_slash_is_allowed(self) -> None:
        self._product("fetch('incidents');")
        report = verify_web_product(self.root, forbid_external_resources=True)
        self.assertEqual(report.errors, [])
        self.assertEqual(report.evidence.same_origin_api_references, 1)

    def test_an_unprovable_target_warns_rather_than_passing_silently(self) -> None:
        """The check cannot show this stays on the product's origin, so it
        must not report it as compliant -- but it is not evidence of a
        violation either, so it is a warning, not an error."""

        self._product("fetch(`${apiBase}/incidents`);")
        report = verify_web_product(self.root, forbid_external_resources=True)
        self.assertEqual(report.errors, [])
        self.assertTrue(report.passed())
        self.assertFalse(report.passed(treat_warnings_as_errors=True))
        self.assertIn(
            WebProductFindingCode.UNPROVEN_REQUEST_TARGET,
            [item.code for item in report.warnings],
        )
        self.assertEqual(report.evidence.dynamic_references_unproven, 1)
        self.assertEqual(report.evidence.same_origin_api_references, 0)
        # An unprovable request is not cross-checked evidence either, so
        # this run is also reported as having established nearly nothing.
        self.assertTrue(report.evidence.is_negligible)
        self.assertIn(
            WebProductFindingCode.NEGLIGIBLE_EVIDENCE,
            [item.code for item in report.warnings],
        )

    def test_an_external_stylesheet_asset_is_still_rejected(self) -> None:
        self.write(
            "index.html",
            '<!doctype html><html><head>'
            '<link rel="stylesheet" href="https://cdn.example.com/x.css">'
            "</head><body></body></html>",
        )
        report = verify_web_product(self.root, forbid_external_resources=True)
        self.assertEqual(
            [item.code for item in report.errors],
            [WebProductFindingCode.EXTERNAL_RESOURCE_REFERENCE],
        )

    def test_the_strict_policy_bans_even_a_same_origin_request(self) -> None:
        """`--forbid-runtime-network-apis` is the pre-ADR-0073 blanket
        behaviour, kept under a name that says so."""

        self._product("fetch('/incidents');")
        relaxed = verify_web_product(self.root, forbid_external_resources=True)
        self.assertTrue(relaxed.passed())
        strict = verify_web_product(self.root, forbid_runtime_network_apis=True)
        self.assertFalse(strict.passed())
        self.assertEqual(
            [item.code for item in strict.errors],
            [WebProductFindingCode.NETWORK_CALL],
        )
        self.assertIn("no runtime requests of any kind", strict.errors[0].detail)

    def test_the_two_policies_are_independent(self) -> None:
        """A product may depend on no third party and still be forbidden to
        make requests, or the reverse. Neither option implies the other."""

        self._product("fetch('https://cdn.example.com/x');")
        external_only = verify_web_product(self.root, forbid_external_resources=True)
        network_only = verify_web_product(self.root, forbid_runtime_network_apis=True)
        both = verify_web_product(
            self.root,
            forbid_external_resources=True,
            forbid_runtime_network_apis=True,
        )
        self.assertEqual(
            [item.code for item in external_only.errors],
            [WebProductFindingCode.CROSS_ORIGIN_REQUEST],
        )
        self.assertEqual(
            [item.code for item in network_only.errors],
            [WebProductFindingCode.NETWORK_CALL],
        )
        self.assertEqual(
            sorted(item.code.value for item in both.errors),
            ["cross_origin_request", "network_call"],
        )

    def test_no_policy_reports_requests_as_information_only(self) -> None:
        self._product(
            "fetch('/incidents');\nfetch('https://cdn.example.com/x');\n"
        )
        report = verify_web_product(self.root)
        self.assertTrue(report.passed())
        self.assertEqual(
            sorted(
                item.code.value
                for item in report.findings
                if item.code
                in {
                    WebProductFindingCode.SAME_ORIGIN_REQUEST,
                    WebProductFindingCode.CROSS_ORIGIN_REQUEST,
                }
            ),
            ["cross_origin_request", "same_origin_request"],
        )


class WebCheckEvidenceReportingTests(WebProductCrossReferenceTests):
    def test_a_real_product_reports_what_it_examined(self) -> None:
        self.write_working_product()
        report = verify_web_product(self.root)
        evidence = report.evidence
        self.assertGreater(evidence.element_references_checked, 0)
        self.assertGreater(evidence.css_selectors_checked, 0)
        # `styles.css` and `app.js` are both referenced from the document.
        self.assertEqual(evidence.local_assets_resolved, 2)
        self.assertFalse(evidence.end_to_end_behavior_measured)
        self.assertFalse(evidence.is_negligible)
        self.assertIn("Static cross-reference only", evidence.ceiling_statement())
        self.assertIn("was NOT measured", evidence.ceiling_statement())

    def test_a_pass_with_no_cross_checked_evidence_says_so(self) -> None:
        """The badge repair on Crisis Atlas replaced computed classes with
        data attributes; the check then passed having cross-checked zero
        element references. That is a valid static result and must not look
        like a UI behavior test."""

        self.write(
            "index.html",
            '<!doctype html><html><body><div data-status="open"></div>'
            '<script src="app.js"></script></body></html>',
        )
        self.write("app.js", "const STATUSES = ['open', 'closed'];")
        report = verify_web_product(self.root)

        self.assertTrue(report.passed())
        self.assertTrue(report.evidence.is_negligible)
        self.assertEqual(report.evidence.element_references_checked, 0)
        self.assertEqual(report.evidence.css_selectors_checked, 0)
        self.assertIn("cross-checked no element references", report.evidence.ceiling_statement())
        self.assertIn(
            "Do not read this pass as evidence", report.evidence.ceiling_statement()
        )
        # And it is surfaced as a warning, so --treat-warnings-as-errors
        # turns "this proved nothing" into a failure for owners who want it.
        self.assertFalse(report.passed(treat_warnings_as_errors=True))
        self.assertIn(
            "cross-checked nothing",
            " ".join(item.detail for item in report.warnings),
        )

    def test_unanalyzed_selectors_count_as_unproven_references(self) -> None:
        self.write(
            "index.html",
            '<!doctype html><html><body><input id="q" type="radio">'
            '<script src="app.js"></script></body></html>',
        )
        self.write(
            "app.js",
            "document.getElementById('q');\n"
            "document.querySelectorAll('input[type=\"radio\"]:checked');\n",
        )
        report = verify_web_product(self.root)
        self.assertEqual(report.unanalyzed_selectors, 1)
        self.assertEqual(report.evidence.dynamic_references_unproven, 1)


class BehavioralCriterionFindingTests(unittest.TestCase):
    """ADR 0073's contract-side half: reading the owner's criterion text
    back to them and asking whether a static check could ever prove it."""

    def test_behavioral_words_are_recognized(self) -> None:
        self.assertEqual(
            behavioral_concerns(
                "A created incident survives a browser reload and a server "
                "restart."
            ),
            [
                "browser/API integration",
                "surviving a reload",
                "surviving a restart",
                "surviving a restart or reload",
            ],
        )
        self.assertEqual(
            behavioral_concerns("Status changes round-trip through the API."),
            ["a round trip through the API", "browser/API integration"],
        )

    def test_a_statically_provable_criterion_is_not_flagged(self) -> None:
        self.assertEqual(
            behavioral_concerns("The export module exposes a to_markdown function."),
            [],
        )

    def test_the_finding_is_a_warning_and_never_changes_the_grade(self) -> None:
        specification = make_specification()
        specification = specification.model_copy(
            update={
                "acceptance_criteria": [
                    AcceptanceCriterion(
                        id="AC-1",
                        text="An incident persists across a browser reload.",
                        source=SourceKind.USER,
                        source_reference="message-1",
                        verification_method="web-product",
                    )
                ]
            }
        )
        commands = [command("web-product", acceptance=True)]
        assessment = assess_verification_contract(
            specification, commands, CompletionPolicy.STRICT
        )

        behavioral = [
            item
            for item in assessment.findings
            if item.code == ContractFindingCode.CRITERION_ASKS_FOR_BEHAVIOR
        ]
        self.assertEqual(len(behavioral), 1)
        self.assertEqual(behavioral[0].severity, ContractFindingSeverity.WARNING)
        self.assertEqual(behavioral[0].subject, "AC-1")
        # Every criterion is still mapped to an acceptance command, so the
        # grade is unchanged: this finding is a question, not a downgrade.
        self.assertEqual(
            assessment.evidence_level, ContractEvidenceLevel.CRITERION_MAPPED
        )
        self.assertTrue(assessment.proves_configured_criteria)


class BehavioralProbeSeamTests(unittest.TestCase):
    def test_requesting_behavioral_proof_fails_rather_than_pretending(self) -> None:
        with self.assertRaises(BrowserProbeUnavailableError) as caught:
            run_behavioral_probe(Path("."))
        self.assertIn("no browser probe provider is implemented", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
