"""MH-2: prompt segment ordering for prefix-cache reuse, and the token ceiling.

Two properties are under test here, and they are related only by the prompt.

The ordering property is about cost: llama-server reuses the KV cache of the
longest common prefix between consecutive requests, so anything volatile
emitted early throws away the prefill of everything after it. The assertions
below are positional on purpose -- they are the only thing standing between
this prompt and someone moving `TURN` back to the top because it reads better
there.

The ceiling property is about correctness: the compiler's char budget and the
model's token window were never reconciled, so an oversized prompt was
silently truncated by the server. These tests assert that a prompt over the
ceiling shrinks in the declared order, that an irreducible one stops with a
named outcome instead of being sent, and that both measurements survive onto
the turn record.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from apoapsis.agent.session import (
    AgentSessionOutcome,
    BoundedAgentSession,
)
from apoapsis.audit.store import TaskAuditStore
from apoapsis.config import AgentLoopConfig
from apoapsis.context.compiler import ContextPackage, evidence_reason_priority
from apoapsis.context.provenance import (
    ContextEvidence,
    EvidenceKind,
    TransmissionPolicy,
)
from apoapsis.context.window import (
    PromptMeasurementSource,
    PromptShrinkStage,
    PromptTooLargeError,
    PromptWindowLimits,
    estimate_tokens,
    fit_prompt_to_window,
    replace_evidence,
)
from apoapsis.models.prompts import (
    agent_step_prompt,
    local_power_step_prompt,
    prompt_static_prefix,
)
from apoapsis.models.tokenize import is_loopback_url
from apoapsis.verification.runner import VerificationConfig
from tests.helpers import make_specification


def evidence(
    identifier: str,
    *,
    reason: str,
    content: str,
    path: str | None = None,
) -> ContextEvidence:
    return ContextEvidence(
        evidence_id=identifier,
        kind=EvidenceKind.FILE_EXCERPT,
        path=path or f"src/{identifier.lower()}.py",
        start_line=1,
        end_line=1 + content.count("\n"),
        commit="0" * 40,
        reason_included=reason,
        content=content,
        transmission_policy=TransmissionPolicy.CLOUD_ALLOWED,
    )


def package_with(evidence_items: list[ContextEvidence]) -> ContextPackage:
    specification = make_specification()
    base = ContextPackage.specification_only(specification, "0" * 40)
    return replace_evidence(base, evidence_items)


class PromptSegmentOrderingTests(unittest.TestCase):
    """The ordering invariant, asserted positionally (MH-2 Task A)."""

    def setUp(self) -> None:
        self.specification = make_specification()
        self.context = package_with(
            [evidence("EV-001", reason="explicit path", content="VALUE = 1\n")]
        )

    def agent_prompt(self, *, turn: int = 3) -> str:
        return agent_step_prompt(
            self.context,
            turn=turn,
            remaining_budgets={"turns": 4, "patch_attempts": 2},
            verification_commands=["tests"],
            history=[{"turn": 1, "action": "inspect_diff", "accepted": True}],
            patch_policy={
                "allow_dependency_changes": False,
                "allow_test_changes": True,
            },
            verification_obligations=["add tests/test_downloads.py"],
            next_action_requirements=["Do not request inspect_diff again."],
        )

    def power_prompt(self, *, turn: int = 3) -> str:
        return local_power_step_prompt(
            self.context,
            turn=turn,
            remaining_budgets={"turns": 4, "seconds": 100},
            verification_commands=["tests"],
            verification_state=[{"name": "tests", "status": "failed"}],
            outstanding_commands=["tests"],
            allowed_project_root="/sandbox",
            forbidden_paths=[".git/**"],
            allowed_shell_prefixes=["python -m unittest"],
            network_enabled=False,
            history=[{"turn": 1, "action": "read_file", "accepted": True}],
            rejected_requests=["read_file .env: forbidden path"],
            acceptance_criteria=["AC-1: downloads resume"],
        )

    @staticmethod
    def body(prompt: str, kind: str) -> str:
        """The dynamic body, with the static prefix removed.

        Required, not cosmetic: the static rules refer to several segment
        headers by name ("VERIFICATION_STATE_JSON below is the authoritative
        record"), so searching the whole prompt for a header finds the
        prefix's mention of it rather than the segment itself.
        """

        prefix = prompt_static_prefix(kind)
        assert prompt.startswith(prefix)
        return prompt[len(prefix) :]

    def assert_order(self, prompt: str, markers: list[str]) -> None:
        positions = []
        for marker in markers:
            self.assertIn(marker, prompt, f"missing segment: {marker}")
            positions.append(prompt.index(marker))
        self.assertEqual(
            positions,
            sorted(positions),
            "segments are emitted out of stable-to-volatile order: "
            + ", ".join(
                f"{marker}@{position}"
                for marker, position in zip(markers, positions)
            ),
        )

    def test_agent_step_prompt_emits_stable_segments_before_volatile_ones(
        self,
    ) -> None:
        self.assert_order(
            self.body(self.agent_prompt(), "agent_step"),
            [
                "TASK_SPECIFICATION_JSON",
                "ACTIVE_HARD_CONSTRAINTS",
                "CONFIGURED_VERIFICATION_COMMANDS_JSON",
                "EFFECTIVE_PATCH_POLICY_JSON",
                "EXTERNAL_RESEARCH_BRIEF",
                "REPOSITORY_EVIDENCE",
                "SESSION_HISTORY_JSON",
                "REQUIRED_VERIFICATION_OBLIGATIONS_JSON",
                "TURN",
                "REMAINING_BUDGETS_JSON",
                "NEXT_ACTION_REQUIREMENTS_JSON",
            ],
        )

    def test_local_power_prompt_emits_stable_segments_before_volatile_ones(
        self,
    ) -> None:
        self.assert_order(
            self.body(self.power_prompt(), "local_power_step"),
            [
                "ALLOWED_PROJECT_ROOT",
                "FORBIDDEN_PATHS_JSON",
                "CONFIGURED_VERIFICATION_COMMANDS_JSON",
                "ACCEPTANCE_CRITERIA_JSON",
                "TASK_SPECIFICATION_JSON",
                "ACTIVE_HARD_CONSTRAINTS",
                "EXTERNAL_RESEARCH_BRIEF",
                "REPOSITORY_EVIDENCE",
                "SESSION_HISTORY_JSON",
                "REFUSED_REQUESTS_JSON",
                "VERIFICATION_STATE_JSON",
                "OUTSTANDING_REQUIRED_COMMANDS_JSON",
                "TURN",
                "REMAINING_BUDGETS_JSON",
            ],
        )

    def test_only_the_volatile_tail_differs_between_consecutive_turns(self) -> None:
        """The point of the reordering, stated as a measurement.

        Two turns that differ only in turn number and budgets must share
        everything up to the volatile tail. Before MH-2 the common prefix
        ended at `TURN`, a few hundred tokens in; now it must reach at least
        the start of the history segment.
        """

        for build, kind in (
            (self.agent_prompt, "agent_step"),
            (self.power_prompt, "local_power_step"),
        ):
            with self.subTest(prompt=kind):
                first = build(turn=1)
                second = build(turn=2)
                shared = 0
                for shared, (left, right) in enumerate(zip(first, second)):
                    if left != right:
                        break
                else:
                    self.fail("the two turns produced an identical prompt")
                history_start = len(prompt_static_prefix(kind)) + self.body(
                    first, kind
                ).index("SESSION_HISTORY_JSON")
                self.assertGreaterEqual(
                    shared,
                    history_start,
                    "the common prefix ends before the history segment, so "
                    "every turn re-prefills the task spec and evidence",
                )

    def test_static_prefix_still_leads_both_prompts(self) -> None:
        self.assertTrue(self.agent_prompt().startswith(prompt_static_prefix("agent_step")))
        self.assertTrue(
            self.power_prompt().startswith(prompt_static_prefix("local_power_step"))
        )


class PromptWindowLimitsTests(unittest.TestCase):
    def test_ceiling_reserves_output_and_margin(self) -> None:
        limits = PromptWindowLimits(
            context_window_tokens=32_768,
            max_output_tokens=8_192,
            safety_margin_tokens=1_024,
        )
        self.assertEqual(limits.prompt_token_ceiling, 32_768 - 8_192 - 1_024)

    def test_absent_window_disables_enforcement_rather_than_guessing(self) -> None:
        self.assertIsNone(
            PromptWindowLimits.from_provider(
                context_window_tokens=None, max_output_tokens=8_192
            )
        )

    def test_a_window_smaller_than_its_own_output_allowance_is_not_enforced(
        self,
    ) -> None:
        # A configuration problem, not a per-turn one: refusing every dispatch
        # would report it once per task instead of once, in doctor.
        self.assertIsNone(
            PromptWindowLimits.from_provider(
                context_window_tokens=4_096, max_output_tokens=8_192
            )
        )
        with self.assertRaises(ValueError):
            PromptWindowLimits(
                context_window_tokens=4_096, max_output_tokens=8_192
            )

    def test_loopback_detection_gates_the_optional_tokenizer(self) -> None:
        self.assertTrue(is_loopback_url("http://127.0.0.1:8080/v1"))
        self.assertTrue(is_loopback_url("http://localhost:8080"))
        self.assertFalse(is_loopback_url("https://api.example.invalid/v1"))
        self.assertFalse(is_loopback_url("not a url"))


class PromptShrinkOrderTests(unittest.TestCase):
    """Task B's ordered reduction: observations, then evidence, then history."""

    def setUp(self) -> None:
        # Deliberately ordered so the compiler priority and the list position
        # disagree -- a guard that dropped the tail would pass a test where
        # they agree.
        self.base = [
            evidence("EV-001", reason="test discovery", content="t" * 400),
            evidence("EV-002", reason="explicit path", content="e" * 400),
            evidence("EV-003", reason="ripgrep match", content="r" * 400),
        ]
        self.observations = [
            evidence("EV-004", reason="bounded read", content="o" * 400),
            evidence("EV-005", reason="bounded read", content="p" * 400),
        ]
        self.history = [
            {
                "turn": index,
                "action": "read_file",
                "accepted": True,
                "summary": "s" * 300,
                "evidence_ids": ["EV-001"] * 20,
            }
            for index in range(1, 4)
        ]
        self.observation_ids = {"EV-004", "EV-005"}

    def build(
        self, items: list[ContextEvidence], turns: list[dict[str, object]]
    ) -> str:
        body = "".join(f"--- {item.evidence_id} \n{item.content}\n" for item in items)
        return f"STATIC_PREFIX\n{body}HISTORY {turns}\n"

    def fit(self, ceiling_tokens: int):
        limits = PromptWindowLimits(
            context_window_tokens=ceiling_tokens + 1_100,
            max_output_tokens=100,
            safety_margin_tokens=1_000,
        )
        self.assertEqual(limits.prompt_token_ceiling, ceiling_tokens)
        return fit_prompt_to_window(
            self.build,
            evidence=[*self.base, *self.observations],
            history=self.history,
            limits=limits,
            is_observation=lambda item: item.evidence_id in self.observation_ids,
        )

    def test_a_prompt_under_the_ceiling_is_left_exactly_alone(self) -> None:
        prompt, fit = self.fit(10_000)
        self.assertEqual(fit.stage, PromptShrinkStage.NONE)
        self.assertFalse(fit.shrank)
        self.assertTrue(fit.fits)
        self.assertEqual(fit.before.prompt_tokens, fit.after.prompt_tokens)
        self.assertEqual(prompt, self.build([*self.base, *self.observations], self.history))

    def test_observations_are_dropped_first_and_oldest_first(self) -> None:
        # Sized so losing one observation is enough.
        full = self.build([*self.base, *self.observations], self.history)
        prompt, fit = self.fit(estimate_tokens(full) - 50)
        self.assertEqual(fit.stage, PromptShrinkStage.OBSERVATIONS)
        self.assertEqual(fit.observations_dropped, 1)
        self.assertEqual(fit.evidence_dropped, 0)
        self.assertEqual(fit.history_turns_compacted, 0)
        self.assertNotIn("--- EV-004 ", prompt)
        self.assertIn("--- EV-005 ", prompt)
        for item in self.base:
            self.assertIn(f"--- {item.evidence_id} ", prompt)

    def test_evidence_is_dropped_by_compiler_priority_not_by_position(self) -> None:
        # Every observation must go before any evidence does, and the first
        # evidence item to go is the least-justified one -- EV-001 ("test
        # discovery"), which is *first* in the list, not last.
        self.assertGreater(
            evidence_reason_priority("test discovery"),
            evidence_reason_priority("ripgrep match"),
        )
        base_only = self.build(self.base, self.history)
        prompt, fit = self.fit(estimate_tokens(base_only) - 50)
        self.assertEqual(fit.stage, PromptShrinkStage.EVIDENCE)
        self.assertEqual(fit.observations_dropped, 2)
        self.assertEqual(fit.evidence_dropped, 1)
        self.assertEqual(fit.history_turns_compacted, 0)
        self.assertNotIn("--- EV-001 ", prompt)
        self.assertIn("--- EV-002 ", prompt)
        self.assertIn("--- EV-003 ", prompt)

    def test_history_is_compacted_last_and_keeps_a_one_line_summary(self) -> None:
        skeleton = self.build([], self.history)
        prompt, fit = self.fit(estimate_tokens(skeleton) - 40)
        self.assertEqual(fit.stage, PromptShrinkStage.HISTORY)
        self.assertEqual(fit.observations_dropped, 2)
        self.assertEqual(fit.evidence_dropped, 3)
        self.assertGreaterEqual(fit.history_turns_compacted, 1)
        # The oldest turn goes first, and it is compacted, not deleted: the
        # model must still be able to see that it already tried this.
        self.assertIn("'compacted': True", prompt)
        self.assertIn("'turn': 1", prompt)
        self.assertIn("'action': 'read_file'", prompt)

    def test_an_irreducible_prompt_raises_with_its_measurement_attached(self) -> None:
        with self.assertRaises(PromptTooLargeError) as caught:
            self.fit(1)
        fit = caught.exception.fit
        self.assertEqual(fit.stage, PromptShrinkStage.IRREDUCIBLE)
        self.assertFalse(fit.fits)
        self.assertEqual(fit.observations_dropped, 2)
        self.assertEqual(fit.evidence_dropped, 3)
        self.assertEqual(fit.history_turns_compacted, len(self.history))
        self.assertGreater(fit.before.prompt_tokens, fit.after.prompt_tokens)
        self.assertIn("cannot be reduced further", str(caught.exception))

    def test_an_exact_tokenizer_is_used_when_offered_and_never_required(self) -> None:
        limits = PromptWindowLimits(
            context_window_tokens=10_000, max_output_tokens=100, safety_margin_tokens=100
        )
        _prompt, fit = fit_prompt_to_window(
            self.build,
            evidence=self.base,
            history=self.history,
            limits=limits,
            count_tokens=lambda _text: 42,
        )
        self.assertEqual(fit.after.measured_by, PromptMeasurementSource.TOKENIZER)
        self.assertEqual(fit.after.prompt_tokens, 42)

        def exploding(_text: str) -> int:
            raise RuntimeError("tokenizer endpoint is down")

        _prompt, fallback = fit_prompt_to_window(
            self.build,
            evidence=self.base,
            history=self.history,
            limits=limits,
            count_tokens=exploding,
        )
        self.assertEqual(fallback.after.measured_by, PromptMeasurementSource.HEURISTIC)
        self.assertTrue(fallback.fits)


class BoundedAgentSessionWindowEnforcementTests(unittest.TestCase):
    """The guard where it actually matters: in front of a live dispatch."""

    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        for argv in (
            ["git", "init", "-b", "main"],
            ["git", "config", "user.email", "tests@example.invalid"],
            ["git", "config", "user.name", "Apoapsis Tests"],
        ):
            subprocess.run(argv, cwd=self.root, check=True, capture_output=True)
        (self.root / ".gitkeep").write_text("", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=self.root, check=True, capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", "baseline"],
            cwd=self.root,
            check=True,
            capture_output=True,
        )
        self.specification = make_specification()

    def session(
        self,
        *,
        context: ContextPackage,
        prompt_window: PromptWindowLimits | None,
        prompts: list[str],
        contexts: list[ContextPackage] | None = None,
        task_id: str = "TASK-WINDOW",
    ) -> BoundedAgentSession:
        def model_call(_operation, prompt, ctx, **_kwargs):
            prompts.append(prompt)
            if contexts is not None:
                contexts.append(ctx)
            return SimpleNamespace(
                content='{"action":"request_escalation","reason":"stop"}'
            )

        return BoundedAgentSession(
            specification=self.specification,
            worktree=self.root,
            initial_context=context,
            context_compiler=SimpleNamespace(compile=lambda *a, **k: context),
            config=AgentLoopConfig(
                max_turns=1,
                max_patch_attempts=1,
                max_verification_runs=1,
                max_search_results=5,
                max_read_lines=50,
                max_observation_chars=5_000,
            ),
            verification_config=VerificationConfig(commands=[]),
            audit=TaskAuditStore(self.root, task_id),
            model_call=model_call,
            apply_patch=lambda patch, attempt: None,
            prompt_window=prompt_window,
        )

    def measure_unguarded(self, context: ContextPackage) -> int:
        """Estimated tokens of the prompt this context produces unguarded.

        Ceilings are derived from a real measured prompt rather than
        hardcoded, so these tests keep testing the shrink *order* when the
        static prefix or the guidance paragraphs change length -- which they
        do, every time a live failure adds a rule.
        """

        prompts: list[str] = []
        self.session(
            context=context, prompt_window=None, prompts=prompts, task_id="TASK-PROBE"
        ).run()
        return estimate_tokens(prompts[0])

    @staticmethod
    def limits_for(ceiling_tokens: int) -> PromptWindowLimits:
        return PromptWindowLimits(
            context_window_tokens=ceiling_tokens + 512,
            max_output_tokens=256,
            safety_margin_tokens=256,
        )

    def test_no_configured_window_means_nothing_is_measured_or_dropped(self) -> None:
        context = package_with(
            [evidence("EV-001", reason="explicit path", content="x" * 40_000)]
        )
        prompts: list[str] = []
        session = self.session(context=context, prompt_window=None, prompts=prompts)
        session.run()
        self.assertIn("--- EV-001 ", prompts[0])
        self.assertIsNone(session.records[-1].prompt_window_fit)

    def test_an_oversized_prompt_shrinks_and_both_measurements_reach_the_record(
        self,
    ) -> None:
        keeper = evidence("EV-001", reason="explicit path", content="a" * 4_000)
        loser = evidence("EV-002", reason="test discovery", content="b" * 4_000)
        context = package_with([keeper, loser])
        # A ceiling that one excerpt fits under and two do not, so the only
        # way through is to give up exactly one -- and which one is the
        # property under test.
        ceiling = self.measure_unguarded(package_with([keeper])) + 8
        self.assertLess(ceiling, self.measure_unguarded(context))

        prompts: list[str] = []
        session = self.session(
            context=context,
            prompt_window=self.limits_for(ceiling),
            prompts=prompts,
        )
        session.run()

        self.assertIn("--- EV-001 ", prompts[0])
        self.assertNotIn("--- EV-002 ", prompts[0])

        fit = session.records[-1].prompt_window_fit
        self.assertIsNotNone(fit)
        assert fit is not None
        self.assertEqual(fit.stage, PromptShrinkStage.EVIDENCE)
        self.assertEqual(fit.evidence_dropped, 1)
        self.assertTrue(fit.fits)
        # Both measurements, not just the surviving one: "we sent 3,000
        # tokens" and "we sent 3,000 after cutting 8,000" are different
        # evidence about the same turn.
        self.assertGreater(fit.before.prompt_tokens, fit.after.prompt_tokens)
        self.assertGreater(fit.before.prompt_tokens, fit.before.token_ceiling)
        self.assertLessEqual(fit.after.prompt_tokens, fit.after.token_ceiling)
        self.assertEqual(fit.before.evidence_items, 2)
        self.assertEqual(fit.after.evidence_items, 1)

    def test_an_irreducible_prompt_stops_the_session_instead_of_being_sent(
        self,
    ) -> None:
        context = package_with(
            [evidence("EV-001", reason="explicit path", content="a" * 4_000)]
        )
        # Below what the static prefix, action protocol, and task
        # specification cost on their own -- nothing the guard is permitted to
        # drop can close this gap.
        ceiling = self.measure_unguarded(package_with([])) - 100
        prompts: list[str] = []
        session = self.session(
            context=context,
            prompt_window=self.limits_for(ceiling),
            prompts=prompts,
        )
        result = session.run()

        self.assertEqual(prompts, [], "an over-window prompt must not be dispatched")
        self.assertEqual(result.outcome, AgentSessionOutcome.ESCALATION_REQUIRED)
        self.assertIn("prompt exceeds model context window", result.stop_reason)
        record = session.records[-1]
        self.assertEqual(record.action, "prompt_window_exceeded")
        self.assertFalse(record.accepted)
        assert record.prompt_window_fit is not None
        self.assertEqual(record.prompt_window_fit.stage, PromptShrinkStage.IRREDUCIBLE)

    def test_the_transmitted_context_matches_the_transmitted_prompt(self) -> None:
        """A dropped excerpt must not be reported as transmitted.

        The context package handed to `model_call` is what the audit and the
        context measurements are computed from, so a guard that shrank the
        prompt but passed the unreduced package would make the harness claim
        it sent evidence the model never saw.
        """

        keeper = evidence("EV-001", reason="explicit path", content="a" * 4_000)
        loser = evidence("EV-002", reason="test discovery", content="b" * 4_000)
        context = package_with([keeper, loser])
        ceiling = self.measure_unguarded(package_with([keeper])) + 8

        prompts: list[str] = []
        transmitted: list[ContextPackage] = []
        session = self.session(
            context=context,
            prompt_window=self.limits_for(ceiling),
            prompts=prompts,
            contexts=transmitted,
            task_id="TASK-WINDOW-CTX",
        )
        session.run()
        self.assertEqual(
            [item.evidence_id for item in transmitted[0].evidence], ["EV-001"]
        )
        self.assertEqual(
            transmitted[0].compiler_parameters["prompt_window_evidence_dropped"], 1
        )


if __name__ == "__main__":
    unittest.main()
