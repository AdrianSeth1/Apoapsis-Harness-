from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from apoapsis.architect.errors import PlanActionError
from apoapsis.architect.schema import PlanStatus, ValidationSeverity
from apoapsis.architect.store import SQLitePlanStore
from apoapsis.architect.validation import validate_plan
from apoapsis.config import (
    ApoapsisConfig,
    ContextCompilerConfig,
    DiscoveryConfig,
    FrontierProviderConfig,
    LocalResearchProviderConfig,
    ModelsConfig,
    ProviderPricing,
)
from apoapsis.discovery.api import (
    FrontierPlanningApiNotConfiguredError,
    preview_frontier_planning_api_call,
    run_frontier_planning_api_call,
)
from apoapsis.discovery.audit import DiscoveryAuditStore
from apoapsis.discovery.errors import (
    AnswerMismatchError,
    BriefNotApprovedError,
    ClarificationRoundCeilingExceededError,
    ConcurrentSessionTransitionError,
    InvalidTransitionError,
    MalformedResponseError,
    PackageIntegrityError,
    ResponseHashMismatchError,
    StaleSessionError,
)
from apoapsis.discovery.frontier_package import (
    build_frontier_planning_request_package,
    load_package,
    verify_package_integrity,
)
from apoapsis.discovery.local_model import (
    DiscoveryModelError,
    propose_clarification_questions,
    propose_idea_brief,
)
from apoapsis.discovery.manual import (
    import_manual_frontier_planning_response,
    write_frontier_planning_artifacts,
)
from apoapsis.discovery.operation_schema import DiscoveryOperationAction
from apoapsis.discovery.operation_service import execute_discovery_operation
from apoapsis.discovery.operation_store import DiscoveryOperationStore
from apoapsis.discovery.schema import (
    ClarificationAnswer,
    ClarificationQuestion,
    DiscoveryStatus,
    FrontierPlanningResponseEnvelope,
)
from apoapsis.discovery.service import (
    approve_idea_brief_step,
    export_frontier_planning_package,
    propose_idea_brief_step,
    propose_local_clarification_questions,
    record_frontier_answers,
    record_local_answers,
    start_session,
)
from apoapsis.discovery.store import SQLiteDiscoveryStore
from apoapsis.evaluation.spend_ceiling import HostedSpendCeilingExceededError
from apoapsis.models.telemetry import InstrumentedModelProvider
from apoapsis.research.schemas import ResearchMode, ResearchTelemetry
from apoapsis.verification.runner import VerificationCommand, VerificationConfig
from tests.architect_helpers import make_plan
from tests.fakes import FakeModelProvider


def _questions_json(count: int) -> str:
    return json.dumps(
        {
            "schema_version": "1.0",
            "questions": [
                {"question_id": f"Q-{index}", "text": f"Question {index}?"}
                for index in range(1, count + 1)
            ],
        }
    )


def _brief_json(*, verbatim_ok: bool = True) -> str:
    verbatim = "Preserve the current public API." if verbatim_ok else "Made up constraint text."
    return json.dumps(
        {
            "schema_version": "1.0",
            "summary": "Add resumable downloads with a pluggable storage backend.",
            "goals": ["Support resuming interrupted downloads."],
            "non_goals": ["Rewriting the storage backend interface."],
            "key_constraints": [
                {
                    "id": "HC-1",
                    "text": "Preserve the current public API.",
                    "verbatim_source": verbatim,
                    "interpreted_meaning": "Do not change public signatures.",
                    "source": "user",
                    "source_reference": "idea",
                    "verification_method": "unit-tests",
                }
            ],
            "open_questions": [],
        }
    )


IDEA_TEXT = "Add resumable downloads with a pluggable storage backend. Preserve the current public API."


class DiscoveryTestsBase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)
        self._git("init", "-b", "main")
        self._git("config", "user.email", "tests@example.invalid")
        self._git("config", "user.name", "Apoapsis Tests")
        (self.root / "README.md").write_text("hi\n", encoding="utf-8")
        self._git("add", ".")
        self._git("commit", "-m", "init")
        (self.root / ".apoapsis").mkdir()
        self.discovery_store = SQLiteDiscoveryStore(
            self.root / ".apoapsis" / "discovery-sessions.db"
        )
        self.plan_store = SQLitePlanStore(self.root / ".apoapsis" / "architect-plans.db")

    def _git(self, *args: str) -> None:
        subprocess.run(
            ["git", *args], cwd=self.root, check=True, capture_output=True, text=True
        )

    def _config(
        self,
        *,
        frontier_coder: FrontierProviderConfig | None = None,
        local_research: LocalResearchProviderConfig | None = None,
    ) -> ApoapsisConfig:
        return ApoapsisConfig(
            models=ModelsConfig(
                frontier=FrontierProviderConfig(
                    base_url="https://provider.invalid/v1", model="fake-local-v1"
                ),
                frontier_coder=frontier_coder,
                local_research=local_research,
            ),
            context=ContextCompilerConfig(
                max_files=10, max_excerpt_lines=200, max_total_chars=50_000
            ),
            verification=VerificationConfig(
                commands=[
                    VerificationCommand(
                        name="unit-tests",
                        category="tests",
                        argv=["python", "-m", "unittest"],
                        timeout_seconds=30,
                    )
                ]
            ),
            discovery=DiscoveryConfig(
                max_clarification_questions=5, max_frontier_clarification_rounds=2
            ),
        )


class LocalModelTests(DiscoveryTestsBase):
    def test_question_count_is_capped_regardless_of_model_output(self) -> None:
        fake = FakeModelProvider([_questions_json(8)])
        provider = InstrumentedModelProvider(fake, ProviderPricing())
        audit = DiscoveryAuditStore(self.root, "DISC-TEST")
        config = self._config()
        questions = propose_clarification_questions(
            provider, config.models.frontier, audit, IDEA_TEXT, max_questions=5
        )
        self.assertEqual(len(questions), 5)

    def test_one_bounded_correction_attempt_recovers(self) -> None:
        fake = FakeModelProvider(["not json at all", _questions_json(3)])
        provider = InstrumentedModelProvider(fake, ProviderPricing())
        audit = DiscoveryAuditStore(self.root, "DISC-TEST")
        questions = propose_clarification_questions(
            provider, self._config().models.frontier, audit, IDEA_TEXT, max_questions=5
        )
        self.assertEqual(len(questions), 3)
        self.assertEqual(len(fake.invocations), 2)

    def test_double_failure_raises(self) -> None:
        fake = FakeModelProvider(["not json", "still not json"])
        provider = InstrumentedModelProvider(fake, ProviderPricing())
        audit = DiscoveryAuditStore(self.root, "DISC-TEST")
        with self.assertRaises(DiscoveryModelError):
            propose_clarification_questions(
                provider, self._config().models.frontier, audit, IDEA_TEXT, max_questions=5
            )
        self.assertEqual(len(fake.invocations), 2)

    def test_brief_verbatim_constraint_check_passes(self) -> None:
        fake = FakeModelProvider([_brief_json(verbatim_ok=True)])
        provider = InstrumentedModelProvider(fake, ProviderPricing())
        audit = DiscoveryAuditStore(self.root, "DISC-TEST")
        brief = propose_idea_brief(provider, self._config().models.frontier, audit, IDEA_TEXT, [])
        self.assertEqual(brief.key_constraints[0].verbatim_source, "Preserve the current public API.")

    def test_brief_verbatim_constraint_check_fails_after_correction(self) -> None:
        fake = FakeModelProvider(
            [_brief_json(verbatim_ok=False), _brief_json(verbatim_ok=False)]
        )
        provider = InstrumentedModelProvider(fake, ProviderPricing())
        audit = DiscoveryAuditStore(self.root, "DISC-TEST")
        with self.assertRaises(DiscoveryModelError):
            propose_idea_brief(provider, self._config().models.frontier, audit, IDEA_TEXT, [])
        self.assertEqual(len(fake.invocations), 2)


class StoreTransitionTests(DiscoveryTestsBase):
    def test_full_happy_path_transitions(self) -> None:
        session = self.discovery_store.create_session("DISC-1", IDEA_TEXT)
        self.assertEqual(session.status, DiscoveryStatus.IDEA_ENTERED)

        questions = [ClarificationQuestion(question_id="Q-1", text="Which storage backend?")]
        session = self.discovery_store.record_local_questions(
            "DISC-1", questions, expected_version=session.version
        )
        self.assertEqual(session.status, DiscoveryStatus.LOCAL_QUESTIONS_PROPOSED)

        answers = [ClarificationAnswer(question_id="Q-1", text="Local disk for now.")]
        session = self.discovery_store.record_local_answers(
            "DISC-1", answers, expected_version=session.version
        )
        self.assertEqual(session.status, DiscoveryStatus.LOCAL_ANSWERS_RECORDED)

        from apoapsis.discovery.schema import IdeaBrief

        brief = IdeaBrief(summary="Add resumable downloads.")
        session = self.discovery_store.record_idea_brief(
            "DISC-1", brief, expected_version=session.version
        )
        self.assertEqual(session.status, DiscoveryStatus.BRIEF_PROPOSED)

        session = self.discovery_store.approve_idea_brief(
            "DISC-1", expected_version=session.version
        )
        self.assertEqual(session.status, DiscoveryStatus.BRIEF_APPROVED)
        self.assertTrue(session.brief_approved)

    def test_stale_version_rejected(self) -> None:
        session = self.discovery_store.create_session("DISC-2", IDEA_TEXT)
        with self.assertRaises(ConcurrentSessionTransitionError):
            self.discovery_store.record_local_questions(
                "DISC-2", [], expected_version=session.version + 1
            )

    def test_invalid_source_status_rejected(self) -> None:
        session = self.discovery_store.create_session("DISC-3", IDEA_TEXT)
        with self.assertRaises(InvalidTransitionError):
            # Cannot approve a brief before one has ever been proposed.
            self.discovery_store.approve_idea_brief("DISC-3", expected_version=session.version)


class PlanningResearchTests(DiscoveryTestsBase):
    class _FakeResearchEngine:
        async def execute(self, specification, requested_mode):
            telemetry = ResearchTelemetry(
                triggered=True,
                trigger_reasons=["explicit test research"],
                effective_mode=requested_mode,
                queries_generated=1,
                sources_searched=[],
                candidates_found=1,
                candidates_after_deduplication=1,
                sources_fetched=1,
                sources_accepted=1,
                sources_rejected=0,
                duplicate_rate=0,
                model_calls=1,
                structured_output_failures=0,
                local_input_tokens=10,
                local_output_tokens=5,
                peak_context_characters=100,
                prompt_injection_flags=0,
                evidence_included=["RSEV-PLAN-1"],
                research_latency_seconds=0.1,
            )
            return SimpleNamespace(
                audit_directory=(
                    f".apoapsis/tasks/{specification.task_id}/research"
                ),
                outcome=SimpleNamespace(
                    brief="Use a durable queue boundary.\n",
                    evidence=[SimpleNamespace(evidence_id="RSEV-PLAN-1")],
                    telemetry=telemetry,
                ),
            )

    def _approved_session(self):
        from apoapsis.discovery.schema import IdeaBrief

        session = self.discovery_store.create_session("DISC-RESEARCH", IDEA_TEXT)
        session = self.discovery_store.record_idea_brief(
            session.session_id,
            IdeaBrief(summary="Design a durable resumable download service."),
            expected_version=session.version,
        )
        return self.discovery_store.approve_idea_brief(
            session.session_id, expected_version=session.version
        )

    def test_research_is_a_durable_pre_planning_operation_and_enters_handoff(self) -> None:
        session = self._approved_session()
        config = self._config(
            local_research=LocalResearchProviderConfig(
                provider="ollama",
                base_url="http://127.0.0.1:11434",
                model="fake-research",
            )
        )
        operation_store = DiscoveryOperationStore(
            self.root / ".apoapsis" / "discovery-operations.db"
        )
        record = execute_discovery_operation(
            self.root,
            self.discovery_store,
            self.plan_store,
            config,
            operation_store,
            session_id=session.session_id,
            action=DiscoveryOperationAction.RESEARCH_FULL,
            operation_id="DISCOP-RESEARCH-1",
            expected_version=session.version,
            research_engine=self._FakeResearchEngine(),
        )
        self.assertEqual(record.status.value, "succeeded")
        researched = self.discovery_store.get_session(session.session_id)
        self.assertEqual(researched.status, DiscoveryStatus.RESEARCH_COMPLETED)
        self.assertEqual(researched.research_mode, ResearchMode.FULL)
        self.assertEqual(researched.research_evidence_ids, ["RSEV-PLAN-1"])

        researched, package, _, markdown_path = export_frontier_planning_package(
            self.root,
            self.discovery_store,
            config,
            researched.session_id,
            transport="manual",
            expected_version=researched.version,
        )
        self.assertTrue(package.research_triggered)
        self.assertEqual(package.research_brief, "Use a durable queue boundary.")
        markdown = (self.root / markdown_path).read_text(encoding="utf-8")
        self.assertIn("## Planning research", markdown)
        self.assertIn("RSEV-PLAN-1", markdown)

    def test_research_operation_refuses_without_configured_research_model(self) -> None:
        session = self._approved_session()
        operation_store = DiscoveryOperationStore(
            self.root / ".apoapsis" / "discovery-operations.db"
        )
        with self.assertRaisesRegex(Exception, "models.local_research"):
            execute_discovery_operation(
                self.root,
                self.discovery_store,
                self.plan_store,
                self._config(),
                operation_store,
                session_id=session.session_id,
                action=DiscoveryOperationAction.RESEARCH_AUTO,
                operation_id="DISCOP-RESEARCH-NO-MODEL",
                expected_version=session.version,
                research_engine=self._FakeResearchEngine(),
            )
        with self.assertRaises(Exception):
            operation_store.get("DISCOP-RESEARCH-NO-MODEL")


class FrontierPackageTests(DiscoveryTestsBase):
    def _approved_session(self):
        config = self._config()
        session = self.discovery_store.create_session("DISC-PKG", IDEA_TEXT)
        from apoapsis.discovery.schema import IdeaBrief, HardConstraint
        from apoapsis.specification.schema import SourceKind

        brief = IdeaBrief(
            summary="Add resumable downloads.",
            key_constraints=[
                HardConstraint(
                    id="HC-1",
                    text="Preserve the current public API.",
                    verbatim_source="Preserve the current public API.",
                    interpreted_meaning="Do not change public signatures.",
                    source=SourceKind.USER,
                    source_reference="idea",
                    verification_method="unit-tests",
                )
            ],
        )
        session = self.discovery_store.record_idea_brief(
            "DISC-PKG", brief, expected_version=session.version
        )
        session = self.discovery_store.approve_idea_brief(
            "DISC-PKG", expected_version=session.version
        )
        return config, session

    def test_package_hash_deterministic_and_tamper_detected(self) -> None:
        config, session = self._approved_session()
        package = build_frontier_planning_request_package(
            self.root,
            config,
            session_id=session.session_id,
            idea_text=session.idea_text,
            idea_brief=session.idea_brief,
            local_questions=[],
            local_answers=[],
            frontier_prior_questions=[],
            frontier_prior_answers=[],
            frontier_round=1,
            package_id="FPKG-FIXED",
        )
        self.assertTrue(verify_package_integrity(package))
        tampered = package.model_copy(update={"idea_text": "different idea"})
        self.assertFalse(verify_package_integrity(tampered))


class FullFlowTests(DiscoveryTestsBase):
    def _to_approved_brief(self, session_id: str):
        config = self._config()
        session = start_session(self.discovery_store, IDEA_TEXT)
        session_id = session.session_id
        fake = FakeModelProvider([_questions_json(2)])
        # propose_local_clarification_questions builds its own provider from
        # config, so patch build_local_provider is unnecessary here -- we
        # call the lower-level function directly with our fake instead.
        from apoapsis.discovery.audit import DiscoveryAuditStore as _Audit
        from apoapsis.discovery.local_model import propose_clarification_questions

        provider = InstrumentedModelProvider(fake, ProviderPricing())
        questions = propose_clarification_questions(
            provider, config.models.frontier, _Audit(self.root, session_id), IDEA_TEXT, max_questions=5
        )
        session = self.discovery_store.record_local_questions(
            session_id, questions, expected_version=session.version
        )
        answers = [ClarificationAnswer(question_id=q.question_id, text="Local disk.") for q in questions]
        session = record_local_answers(
            self.discovery_store, session_id, answers, expected_version=session.version
        )
        fake_brief = FakeModelProvider([_brief_json(verbatim_ok=True)])
        brief_provider = InstrumentedModelProvider(fake_brief, ProviderPricing())
        from apoapsis.discovery.local_model import propose_idea_brief as _propose_brief

        brief = _propose_brief(
            brief_provider, config.models.frontier, _Audit(self.root, session_id), IDEA_TEXT, answers
        )
        session = self.discovery_store.record_idea_brief(
            session_id, brief, expected_version=session.version
        )
        session = approve_idea_brief_step(
            self.discovery_store, session_id, expected_version=session.version
        )
        return config, session

    def test_manual_transport_plan_response_completes_and_reuses_existing_plan_machinery(
        self,
    ) -> None:
        config, session = self._to_approved_brief("DISC-FLOW-1")
        session, package, json_path, markdown_path = export_frontier_planning_package(
            self.root,
            self.discovery_store,
            config,
            session.session_id,
            transport="manual",
            expected_version=session.version,
        )
        self.assertTrue((self.root / json_path).is_file())
        self.assertTrue((self.root / markdown_path).is_file())
        markdown = (self.root / markdown_path).read_text(encoding="utf-8")
        self.assertIn(package.package_id, markdown)

        plan = make_plan()
        envelope = {
            "schema_version": "1.0",
            "package_id": package.package_id,
            "package_sha256": package.package_sha256,
            "session_id": session.session_id,
            "kind": "plan",
            "plan": json.loads(plan.model_dump_json()),
        }
        session = import_manual_frontier_planning_response(
            self.root,
            self.discovery_store,
            self.plan_store,
            config,
            session_id=session.session_id,
            package_id=package.package_id,
            response_bytes=json.dumps(envelope).encode("utf-8"),
            declared_model_name="claude-opus-4.6-web",
        )
        self.assertEqual(session.status, DiscoveryStatus.PLAN_IMPORTED)
        self.assertIsNotNone(session.plan_id)

        # The resulting plan continues through the existing, completely
        # unmodified Architect Mode validate/approve machinery.
        record = self.plan_store.get_plan(session.plan_id)
        self.assertEqual(record.status, PlanStatus.PROPOSED)
        findings = validate_plan(
            record.plan,
            configured_verification_commands={"unit-tests"},
            ceilings=config.architect.ceilings,
        )
        result_valid = not any(item.severity == ValidationSeverity.ERROR for item in findings)
        self.assertTrue(result_valid)
        from apoapsis.architect.schema import PlanValidationResult

        validation = PlanValidationResult(
            plan_id=record.plan_id, plan_version=record.version, valid=True, findings=[]
        )
        updated = self.plan_store.record_validation(
            record.plan_id, validation, expected_version=record.version
        )
        approved = self.plan_store.approve_plan(record.plan_id, expected_version=updated.version)
        self.assertEqual(approved.status, PlanStatus.APPROVED)

    def test_stale_package_response_rejected(self) -> None:
        config, session = self._to_approved_brief("DISC-FLOW-2")
        session, package, _, _ = export_frontier_planning_package(
            self.root,
            self.discovery_store,
            config,
            session.session_id,
            transport="manual",
            expected_version=session.version,
        )
        envelope = {
            "schema_version": "1.0",
            "package_id": "FPKG-DOES-NOT-EXIST",
            "package_sha256": "0" * 64,
            "session_id": session.session_id,
            "kind": "clarification_questions",
            "clarification_questions": [{"question_id": "Q-1", "text": "?"}],
        }
        with self.assertRaises(Exception):
            import_manual_frontier_planning_response(
                self.root,
                self.discovery_store,
                self.plan_store,
                config,
                session_id=session.session_id,
                package_id="FPKG-DOES-NOT-EXIST",
                response_bytes=json.dumps(envelope).encode("utf-8"),
                declared_model_name="claude-opus-4.6-web",
            )

    def test_response_hash_mismatch_rejected(self) -> None:
        config, session = self._to_approved_brief("DISC-FLOW-3")
        session, package, _, _ = export_frontier_planning_package(
            self.root,
            self.discovery_store,
            config,
            session.session_id,
            transport="manual",
            expected_version=session.version,
        )
        envelope = {
            "schema_version": "1.0",
            "package_id": package.package_id,
            "package_sha256": "0" * 64,
            "session_id": session.session_id,
            "kind": "clarification_questions",
            "clarification_questions": [{"question_id": "Q-1", "text": "?"}],
        }
        with self.assertRaises(ResponseHashMismatchError):
            import_manual_frontier_planning_response(
                self.root,
                self.discovery_store,
                self.plan_store,
                config,
                session_id=session.session_id,
                package_id=package.package_id,
                response_bytes=json.dumps(envelope).encode("utf-8"),
                declared_model_name="claude-opus-4.6-web",
            )

    def test_malformed_response_rejected(self) -> None:
        config, session = self._to_approved_brief("DISC-FLOW-4")
        session, package, _, _ = export_frontier_planning_package(
            self.root,
            self.discovery_store,
            config,
            session.session_id,
            transport="manual",
            expected_version=session.version,
        )
        with self.assertRaises(MalformedResponseError):
            import_manual_frontier_planning_response(
                self.root,
                self.discovery_store,
                self.plan_store,
                config,
                session_id=session.session_id,
                package_id=package.package_id,
                response_bytes=b"not json {{{",
                declared_model_name="claude-opus-4.6-web",
            )

    def test_clarification_round_ceiling_enforced(self) -> None:
        config, session = self._to_approved_brief("DISC-FLOW-5")
        for round_number in range(1, 4):
            session, package, _, _ = export_frontier_planning_package(
                self.root,
                self.discovery_store,
                config,
                session.session_id,
                transport="manual",
                expected_version=session.version,
            )
            envelope = {
                "schema_version": "1.0",
                "package_id": package.package_id,
                "package_sha256": package.package_sha256,
                "session_id": session.session_id,
                "kind": "clarification_questions",
                "clarification_questions": [
                    {"question_id": f"Q-{round_number}", "text": "Another question?"}
                ],
            }
            if round_number <= config.discovery.max_frontier_clarification_rounds:
                session = import_manual_frontier_planning_response(
                    self.root,
                    self.discovery_store,
                    self.plan_store,
                    config,
                    session_id=session.session_id,
                    package_id=package.package_id,
                    response_bytes=json.dumps(envelope).encode("utf-8"),
                    declared_model_name="claude-opus-4.6-web",
                )
                self.assertEqual(
                    session.status, DiscoveryStatus.FRONTIER_CLARIFICATION_PROPOSED
                )
                answers = [
                    ClarificationAnswer(question_id=q.question_id, text="Answer.")
                    for q in session.frontier_questions
                ]
                session = record_frontier_answers(
                    self.discovery_store,
                    session.session_id,
                    answers,
                    expected_version=session.version,
                )
            else:
                with self.assertRaises(ClarificationRoundCeilingExceededError):
                    import_manual_frontier_planning_response(
                        self.root,
                        self.discovery_store,
                        self.plan_store,
                        config,
                        session_id=session.session_id,
                        package_id=package.package_id,
                        response_bytes=json.dumps(envelope).encode("utf-8"),
                        declared_model_name="claude-opus-4.6-web",
                    )

    def test_answer_mismatch_rejected(self) -> None:
        config, session = self._to_approved_brief("DISC-FLOW-6")
        with self.assertRaises(AnswerMismatchError):
            record_frontier_answers(
                self.discovery_store,
                session.session_id,
                [ClarificationAnswer(question_id="Q-NOT-REAL", text="x")],
                expected_version=session.version,
            )

    def test_export_before_brief_approval_rejected(self) -> None:
        config = self._config()
        session = start_session(self.discovery_store, IDEA_TEXT)
        with self.assertRaises(BriefNotApprovedError):
            export_frontier_planning_package(
                self.root,
                self.discovery_store,
                config,
                session.session_id,
                transport="manual",
                expected_version=session.version,
            )


class ApiTransportTests(DiscoveryTestsBase):
    def _approved_export(self, *, cost_usd: float = 0.001):
        frontier_coder = FrontierProviderConfig(
            provider="ollama",
            base_url="http://127.0.0.1:11434",
            model="fake-frontier-planner",
            pricing=ProviderPricing(
                input_per_million_usd=1.0, output_per_million_usd=1.0
            ),
        )
        config = self._config(frontier_coder=frontier_coder)
        session = start_session(self.discovery_store, IDEA_TEXT)
        from apoapsis.discovery.schema import IdeaBrief

        session = self.discovery_store.record_idea_brief(
            session.session_id, IdeaBrief(summary="Add resumable downloads."),
            expected_version=session.version,
        )
        session = approve_idea_brief_step(
            self.discovery_store, session.session_id, expected_version=session.version
        )
        session, package, _, _ = export_frontier_planning_package(
            self.root,
            self.discovery_store,
            config,
            session.session_id,
            transport="api",
            expected_version=session.version,
        )
        return config, session, package

    def test_not_configured_raises(self) -> None:
        config = self._config()
        session = start_session(self.discovery_store, IDEA_TEXT)
        with self.assertRaises(FrontierPlanningApiNotConfiguredError):
            from apoapsis.discovery.frontier_package import build_frontier_planning_request_package
            from apoapsis.discovery.schema import IdeaBrief

            package = build_frontier_planning_request_package(
                self.root,
                config,
                session_id=session.session_id,
                idea_text=session.idea_text,
                idea_brief=IdeaBrief(summary="x"),
                local_questions=[],
                local_answers=[],
                frontier_prior_questions=[],
                frontier_prior_answers=[],
                frontier_round=1,
            )
            preview_frontier_planning_api_call(config, package)

    def test_preview_shows_provider_model_and_worst_case(self) -> None:
        config, session, package = self._approved_export()
        preview = preview_frontier_planning_api_call(config, package)
        self.assertEqual(preview.model, "fake-frontier-planner")
        self.assertGreater(preview.worst_case_call_cost_usd, 0.0)

    def test_spend_ceiling_refuses_before_call_when_worst_case_exceeds(self) -> None:
        config, session, package = self._approved_export()
        fake = FakeModelProvider(["should never be reached"])
        provider = InstrumentedModelProvider(fake, config.models.frontier_coder.pricing)
        with self.assertRaises(HostedSpendCeilingExceededError):
            run_frontier_planning_api_call(
                self.root,
                self.discovery_store,
                self.plan_store,
                config,
                session_id=session.session_id,
                package=package,
                authorized_max_spend_usd=0.0000001,
                frontier_coder_provider=provider,
            )
        self.assertEqual(len(fake.invocations), 0)

    def test_successful_api_call_persists_measured_cost_and_completes(self) -> None:
        config, session, package = self._approved_export()
        plan = make_plan()
        envelope = json.dumps(
            {
                "schema_version": "1.0",
                "package_id": package.package_id,
                "package_sha256": package.package_sha256,
                "session_id": session.session_id,
                "kind": "plan",
                "plan": json.loads(plan.model_dump_json()),
            }
        )
        fake = FakeModelProvider([envelope])
        provider = InstrumentedModelProvider(fake, config.models.frontier_coder.pricing)
        updated_session, cost_usd = run_frontier_planning_api_call(
            self.root,
            self.discovery_store,
            self.plan_store,
            config,
            session_id=session.session_id,
            package=package,
            authorized_max_spend_usd=10.0,
            frontier_coder_provider=provider,
        )
        self.assertEqual(updated_session.status, DiscoveryStatus.PLAN_IMPORTED)
        self.assertGreater(cost_usd, 0.0)
        self.assertEqual(len(fake.invocations), 1)


if __name__ == "__main__":
    unittest.main()
