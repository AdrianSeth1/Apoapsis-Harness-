from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from apoapsis.config import ContextCompilerConfig
from apoapsis.context.compiler import ContextCompiler, ContextPackage
from apoapsis.context.measurement import attribute_context_to_patch, measure_context
from apoapsis.context.provenance import ContextEvidence, EvidenceKind, TransmissionPolicy
from tests.helpers import make_specification


def _evidence(
    evidence_id: str,
    *,
    path: str = "src/a.py",
    start: int = 1,
    end: int = 5,
    content: str = "x" * 40,
    kind: EvidenceKind = EvidenceKind.FILE_EXCERPT,
) -> ContextEvidence:
    return ContextEvidence(
        evidence_id=evidence_id,
        kind=kind,
        path=path,
        start_line=start,
        end_line=end,
        commit="deadbeef",
        reason_included="test",
        content=content,
        transmission_policy=TransmissionPolicy.CLOUD_ALLOWED,
    )


def _package(evidence: list[ContextEvidence], **parameters: object) -> ContextPackage:
    specification = make_specification()
    return ContextPackage(
        task_id=specification.task_id,
        specification=specification,
        head_commit="deadbeef",
        query_terms=[],
        retrieval_tools=["git"],
        compiler_parameters=parameters,
        evidence=evidence,
    )


class MeasureContextUnitTests(unittest.TestCase):
    def test_everything_is_new_without_a_previous_package(self) -> None:
        package = _package(
            [_evidence("EV-001")], max_files=16, max_excerpt_lines=160
        )
        measurement = measure_context(package, model_context_window_tokens=65536)

        self.assertEqual(measurement.new_evidence_count, 1)
        self.assertEqual(measurement.stable_evidence_count, 0)
        self.assertEqual(measurement.new_evidence_chars, 40)
        self.assertEqual(measurement.total_transmitted_chars, 40)
        self.assertEqual(measurement.repository_file_limit, 16)
        self.assertEqual(measurement.excerpt_line_limit, 160)
        self.assertEqual(measurement.files_included, 1)
        self.assertEqual(measurement.estimated_tokens, 10)  # 40 chars / 4
        self.assertAlmostEqual(measurement.model_window_utilization, 10 / 65536)

    def test_stable_versus_new_evidence_is_an_identity_key_diff(self) -> None:
        stable_item = _evidence("EV-001", path="src/a.py")
        new_item = _evidence("EV-002", path="src/b.py", content="y" * 20)
        previous = _package([stable_item])
        current = _package([stable_item, new_item])

        measurement = measure_context(current, previous_package=previous)

        self.assertEqual(measurement.stable_evidence_count, 1)
        self.assertEqual(measurement.new_evidence_count, 1)
        self.assertEqual(measurement.stable_evidence_chars, 40)
        self.assertEqual(measurement.new_evidence_chars, 20)

    def test_a_changed_content_hash_is_new_not_stable(self) -> None:
        # same path/lines, different content -> different content_sha256 ->
        # a different identity, correctly counted as new.
        previous = _package([_evidence("EV-001", content="original content" * 3)])
        current = _package([_evidence("EV-002", content="different content" * 3)])

        measurement = measure_context(current, previous_package=previous)

        self.assertEqual(measurement.stable_evidence_count, 0)
        self.assertEqual(measurement.new_evidence_count, 1)

    def test_composition_breaks_down_by_evidence_kind(self) -> None:
        package = _package(
            [
                _evidence("EV-001", kind=EvidenceKind.FILE_EXCERPT, content="a" * 10),
                _evidence("EV-002", kind=EvidenceKind.FILE_EXCERPT, content="b" * 5),
                _evidence("EV-003", path="tests/test_a.py", kind=EvidenceKind.TEST, content="c" * 8),
            ]
        )
        measurement = measure_context(package)

        by_kind = {item.kind: item for item in measurement.composition}
        self.assertEqual(by_kind[EvidenceKind.FILE_EXCERPT].item_count, 2)
        self.assertEqual(by_kind[EvidenceKind.FILE_EXCERPT].char_count, 15)
        self.assertEqual(by_kind[EvidenceKind.TEST].item_count, 1)
        self.assertEqual(by_kind[EvidenceKind.TEST].char_count, 8)

    def test_synthetic_paths_are_excluded_from_files_included(self) -> None:
        package = _package(
            [
                _evidence("EV-001", path="src/a.py"),
                _evidence(
                    "EV-002",
                    path="<working-tree-diff>",
                    kind=EvidenceKind.DIFF,
                    start=1,
                    end=1,
                ),
            ]
        )
        measurement = measure_context(package)
        self.assertEqual(measurement.files_included, 1)

    def test_no_model_window_means_no_utilization(self) -> None:
        package = _package([_evidence("EV-001")])
        measurement = measure_context(package, model_context_window_tokens=None)
        self.assertIsNone(measurement.model_window_utilization)

    def test_truncation_counters_pass_through_from_compiler_parameters(self) -> None:
        package = _package(
            [_evidence("EV-001")],
            candidate_file_count=9,
            files_truncated_by_limit=3,
            files_dropped_for_char_budget=2,
            excerpts_truncated_for_char_budget=1,
        )
        measurement = measure_context(package)
        self.assertEqual(measurement.candidate_file_count, 9)
        self.assertEqual(measurement.files_truncated_by_limit, 3)
        self.assertEqual(measurement.files_dropped_for_char_budget, 2)
        self.assertEqual(measurement.excerpts_truncated_for_char_budget, 1)

    def test_agent_observation_budget_falls_back_to_compiler_parameters(self) -> None:
        package = _package(
            [_evidence("EV-001")],
            agent_loop={"max_observation_chars": 48_000, "max_turns": 12},
        )
        measurement = measure_context(package)
        self.assertEqual(measurement.agent_observation_budget_chars, 48_000)

    def test_explicit_agent_observation_budget_overrides_compiler_parameters(self) -> None:
        package = _package(
            [_evidence("EV-001")],
            agent_loop={"max_observation_chars": 48_000},
        )
        measurement = measure_context(
            package, agent_observation_budget_chars=99_000
        )
        self.assertEqual(measurement.agent_observation_budget_chars, 99_000)

    def test_one_shot_context_has_no_observation_budget(self) -> None:
        package = _package([_evidence("EV-001")])
        measurement = measure_context(package)
        self.assertIsNone(measurement.agent_observation_budget_chars)

    def test_observation_compaction_counters_pass_through(self) -> None:
        package = _package(
            [_evidence("EV-001")],
            observation_ledger_items=5,
            observation_ledger_chars=5000,
            observation_transmitted_items=2,
            observation_transmitted_chars=1800,
            observations_compacted_count=3,
            observations_compacted_chars=3200,
        )

        measurement = measure_context(package)

        self.assertEqual(measurement.observation_ledger_items, 5)
        self.assertEqual(measurement.observation_transmitted_chars, 1800)
        self.assertEqual(measurement.observations_compacted_count, 3)
        self.assertEqual(measurement.observations_compacted_chars, 3200)

    def test_context_attribution_is_file_level_and_requires_acceptance(self) -> None:
        package = _package(
            [
                _evidence("EV-001", path="src/a.py", content="a" * 20),
                _evidence("EV-002", path="tests/test_a.py", content="t" * 30),
            ]
        )

        accepted = attribute_context_to_patch(
            [package], changed_files=["src/a.py"], accepted_patch=True
        )
        rejected = attribute_context_to_patch(
            [package], changed_files=["src/a.py"], accepted_patch=False
        )

        self.assertEqual(accepted.attributed_chars, 20)
        self.assertEqual(accepted.transmitted_chars, 50)
        self.assertEqual(accepted.signal_density_ratio, 0.4)
        self.assertIsNone(rejected.signal_density_ratio)
        self.assertIn("did not complete", rejected.reason or "")


class CompilerInstrumentationIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)
        (self.root / "downloader_alpha.py").write_text(
            "def download_resource():\n    return 'alpha'\n" + ("# pad\n" * 300),
            encoding="utf-8",
        )
        (self.root / "downloader_beta.py").write_text(
            "def download_resource_helper():\n    return 'beta'\n" + ("# pad\n" * 300),
            encoding="utf-8",
        )
        self._git("init", "-b", "main")
        self._git("config", "user.email", "tests@example.invalid")
        self._git("config", "user.name", "Apoapsis Tests")
        self._git("add", ".")
        self._git("commit", "-m", "fixture")

    def _git(self, *args: str) -> None:
        subprocess.run(
            ["git", *args], cwd=self.root, check=True, capture_output=True, text=True
        )

    def test_candidate_count_and_file_limit_truncation_are_measured(self) -> None:
        compiler = ContextCompiler(ContextCompilerConfig(max_files=1))
        specification = make_specification()

        package = compiler.compile(specification, self.root)

        self.assertGreaterEqual(package.compiler_parameters["candidate_file_count"], 2)
        self.assertGreaterEqual(
            package.compiler_parameters["files_truncated_by_limit"], 1
        )
        measurement = measure_context(package, model_context_window_tokens=65536)
        self.assertEqual(measurement.files_truncated_by_limit, measurement.candidate_file_count - 1)

    def test_char_budget_exhaustion_is_measured(self) -> None:
        compiler = ContextCompiler(
            ContextCompilerConfig(
                max_files=10, max_total_chars=1_000, max_excerpt_lines=1000
            )
        )
        specification = make_specification()

        package = compiler.compile(specification, self.root)
        measurement = measure_context(package)

        self.assertTrue(
            measurement.excerpts_truncated_for_char_budget > 0
            or measurement.files_dropped_for_char_budget > 0
        )
        self.assertLessEqual(measurement.total_transmitted_chars, 1_000)

    def test_recompiling_identical_input_is_fully_stable(self) -> None:
        compiler = ContextCompiler(ContextCompilerConfig(max_files=10))
        specification = make_specification()

        first = compiler.compile(specification, self.root)
        second = compiler.compile(specification, self.root)

        measurement = measure_context(second, previous_package=first)
        self.assertEqual(measurement.new_evidence_count, 0)
        self.assertEqual(measurement.stable_evidence_count, len(second.evidence))
        self.assertGreater(measurement.stable_evidence_count, 0)


if __name__ == "__main__":
    unittest.main()
