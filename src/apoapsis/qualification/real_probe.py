"""Real qualification: clones on disk, commands that run, witnesses emitted.

7P.1b shipped `PackageProbe` with one implementation -- a fake -- and reported
its green result as though the package had been qualified. The fake proves the
validator branches correctly. It cannot prove that the Crisis Atlas seed clones
to the tree it names, that the inherited suite really passes without reaching
the declared services, or that the historical candidate really is refused by
the authoritative checkpoint. Those are claims about bytes and processes, and
only bytes and processes settle them.

This module supplies the missing half. It clones the seed for real, applies
candidates for real, runs the seed's own suite for real, and drives the same
`run_checkpoint` the Capability Sandbox uses -- not a reimplementation of it,
because a second implementation would be a second thing to be wrong.

**Coverage comes from the standard library.** `trace` rather than `coverage.py`,
because the harness declares only `pydantic` as a runtime dependency, and a
proof that reports `unrun` on a host without an optional package is a proof
that will usually report `unrun`. `collection_method` on the witness records
which tool measured, which is exactly the field's purpose.

**Offline by construction.** Nothing here opens a socket: the commands are
`unittest` runs over a local clone. The subprocess environment is scrubbed of
proxy variables and given `PYTHONDONTWRITEBYTECODE=1` so a run cannot leave
byproducts that the next admission would see as authored work (ADR 0063). This
is process-level offline, not a network namespace; the namespace-enforced
boundary is the Docker workcell of ADR 0077, which no proof here requires.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from apoapsis.qualification.case_package import (
    CheckpointObservation,
    CommandOutcome,
    EvidenceKind,
    GitCloneObserver,
    SeedObservation,
)
from apoapsis.workcell.acceptance import (
    AcceptanceObligation,
    ObligationKind,
    ObligationStatus,
    SliceAcceptanceContract,
)
from apoapsis.workcell.checkpoint import run_checkpoint
from apoapsis.workcell.emitters import emit_test_witness

#: Evidence identities that differ between two correct runs. Declared here so
#: proof 8 excludes exactly these and nothing else. A temporary directory name
#: and a wall-clock duration are not properties of the package.
VOLATILE_EVIDENCE_FIELDS: tuple[str, ...] = ("workspace", "duration_seconds")

#: Written into a temp directory and executed as the verification command.
#: The controller owns it: it decides where the artifact goes, deletes it
#: first (inside `emit_test_witness`), and hashes what it reads back.
_TRACING_RUNNER = '''\
"""Run a project's own unittest suite and record which lines executed.

Emits coverage.py's JSON report shape from the standard library `trace`
module, so the witness pipeline is unchanged and the measurement carries no
optional dependency.
"""
import json
import os
import sys
import trace
import unittest

artifact = sys.argv[1]
root = os.getcwd()

# `python -m unittest` puts the working directory on sys.path; a script run by
# path does not -- sys.path[0] is the script's own directory. Without this the
# project under test is simply not importable, every test errors, and the
# result reads as "the seed's suite is red" rather than "the runner was wrong".
if root not in sys.path:
    sys.path.insert(0, root)

tracer = trace.Trace(
    count=1, trace=0, ignoredirs=[sys.prefix, sys.exec_prefix]
)
outcome = {}


def _run():
    suite = unittest.TestLoader().discover("tests", top_level_dir=root)
    runner = unittest.TextTestRunner(verbosity=0, stream=sys.stderr)
    outcome["result"] = runner.run(suite)


tracer.runfunc(_run)

files = {}
for (filename, lineno), hits in tracer.results().counts.items():
    if not hits:
        continue
    # `<frozen importlib._bootstrap>` and friends are not files. Reporting
    # them as covered paths would put pseudo-entries in the evidence that no
    # path check can ever resolve.
    if filename.startswith("<") or not os.path.isfile(filename):
        continue
    try:
        relative = os.path.relpath(os.path.realpath(filename), root)
    except ValueError:
        continue
    if relative.startswith(os.pardir) or os.path.isabs(relative):
        continue
    files.setdefault(relative.replace(os.sep, "/"), set()).add(lineno)

with open(artifact, "w", encoding="utf-8") as handle:
    json.dump(
        {
            "files": {
                path: {"executed_lines": sorted(lines)}
                for path, lines in sorted(files.items())
            }
        },
        handle,
        sort_keys=True,
    )

sys.exit(0 if outcome["result"].wasSuccessful() else 1)
'''


def _clean_environment() -> dict[str, str]:
    environment = {
        key: value
        for key, value in os.environ.items()
        if key.lower() not in {"http_proxy", "https_proxy", "all_proxy", "ftp_proxy"}
    }
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    environment["PYTHONHASHSEED"] = "0"
    environment["NO_PROXY"] = "*"
    return environment


class RealCasePackageProbe:
    """Clones, applies, runs, and reports what actually happened.

    One instance serves a whole validation. Each `run_checkpoint` call gets its
    own pristine clone as the base and its own candidate tree, so no proof can
    observe a tree another proof mutated -- which matters most for proof 5,
    where the point is that removing one artifact changes exactly one outcome.

    `evidence_root` is where raw evidence is persisted. It is a caller-supplied
    directory outside any ephemeral workspace on purpose: a qualification whose
    evidence vanished with its temp directory would leave the claim and delete
    the reason to believe it.
    """

    evidence_kind = EvidenceKind.REAL_QUALIFICATION

    def __init__(
        self,
        *,
        seed_repository: Path,
        package_root: Path,
        evidence_root: Path,
        python_executable: str | None = None,
    ) -> None:
        self.seed_repository = Path(seed_repository).resolve()
        self.package_root = Path(package_root).resolve()
        self.evidence_root = Path(evidence_root).resolve()
        self.evidence_root.mkdir(parents=True, exist_ok=True)
        self.python = python_executable or sys.executable
        self._observer = GitCloneObserver(self.seed_repository)
        self._runner_script = self.evidence_root / "tracing_runner.py"
        self._runner_script.write_text(_TRACING_RUNNER, encoding="utf-8")
        self._checkpoints = 0

    # -- seed inspection ----------------------------------------------------

    def clone_seed(self, *, destination: Path) -> SeedObservation:
        return self._observer.clone_seed(destination=destination)

    def read_seed_paths(self, *, destination: Path) -> tuple[str, ...]:
        return self._observer.read_seed_paths(destination=destination)

    def search_seed_symbols(
        self, *, destination: Path, symbols: tuple[str, ...]
    ) -> tuple[str, ...]:
        return self._observer.search_seed_symbols(
            destination=destination, symbols=symbols
        )

    # -- running the seed's own suite ---------------------------------------

    def _run_traced_suite(self, tree: Path, artifact: Path) -> tuple[int, list[str]]:
        artifact.parent.mkdir(parents=True, exist_ok=True)
        artifact.unlink(missing_ok=True)
        completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
            [self.python, str(self._runner_script), str(artifact)],
            cwd=tree,
            capture_output=True,
            text=True,
            env=_clean_environment(),
            timeout=600,
        )
        # Kept whatever the exit code was. A run that failed is exactly the run
        # whose output someone will need, and re-running it to find out why is
        # how a transient cause gets lost.
        (artifact.parent / f"{artifact.stem}-output.txt").write_text(
            f"exit_code: {completed.returncode}\n"
            f"--- stdout ---\n{completed.stdout}\n"
            f"--- stderr ---\n{completed.stderr}\n",
            encoding="utf-8",
        )
        covered: list[str] = []
        if artifact.is_file():
            payload = json.loads(artifact.read_text(encoding="utf-8"))
            covered = sorted(payload.get("files", {}))
        return completed.returncode, covered

    def run_inherited_suite(self, *, destination: Path) -> CommandOutcome:
        """Run the seed's suite exactly as inherited, and record what it reached."""

        artifact = self.evidence_root / "inherited-coverage.json"
        exit_code, covered = self._run_traced_suite(destination, artifact)
        (self.evidence_root / "inherited-suite.json").write_text(
            json.dumps(
                {
                    "command": "python tracing_runner.py (unittest discover -s tests)",
                    "exit_code": exit_code,
                    "covered_paths": covered,
                    "coverage_artifact": artifact.name,
                    "collection_method": "stdlib trace module",
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        return CommandOutcome(
            name="unit-tests", exit_code=exit_code, covered_paths=tuple(covered)
        )


    # -- candidates ---------------------------------------------------------

    def _candidate_files(self, candidate: str) -> dict[str, bytes]:
        """The files one candidate writes, keyed by path in the project tree."""

        source = self.package_root / "evaluator-only" / (
            "reference" if candidate == "reference" else "incomplete"
        )
        collected: dict[str, bytes] = {}
        for path in sorted(source.rglob("*")):
            if not path.is_file():
                continue
            relative = path.relative_to(source).as_posix()
            if relative == "provenance.json":
                # Evaluator provenance, not part of what the model proposed.
                continue
            collected[relative] = path.read_bytes()
        if not collected:
            raise RuntimeError(f"candidate {candidate!r} contributed no files")
        return collected

    def _acceptance_claimed_criteria(self) -> list[str]:
        """Criteria the acceptance-designated command is configured to prove.

        This is owner configuration, read out of the package, not the emitter
        grading its own work. A claim is necessary but nowhere near sufficient:
        the obligation still requires the declared path to exist and the hashed
        coverage artifact to report executed lines inside it, which is why
        removing `export_service.py` fails `AC-EXPORT-SERVICE` in proof 5 even
        though the witness goes on claiming it.

        Only `test_coverage` criteria are claimed. `AC-INHERITED-INSUFFICIENT`
        is a negative control -- a rule about what may *not* prove a criterion
        -- and a witness claiming it would be asserting the very substitution
        the rule forbids.
        """

        payload = json.loads(
            (self.package_root / "acceptance-criteria.json").read_text(
                encoding="utf-8"
            )
        )
        return [
            item["criterion_id"]
            for item in payload["criteria"]
            if item["required_witness_kind"] == "test_coverage"
        ]

    def _contract(self) -> SliceAcceptanceContract:
        payload = json.loads(
            (self.package_root / "plan-contract.json").read_text(encoding="utf-8")
        )
        return SliceAcceptanceContract(
            slice_id=payload["slice_id"],
            criteria=list(payload["criteria"]),
            obligations=[
                AcceptanceObligation(
                    obligation_id=item["obligation_id"],
                    kind=ObligationKind(item["kind"]),
                    description=item["description"],
                    required_paths=list(item["required_paths"]),
                    must_be_exercised=list(item["must_be_exercised"]),
                    criteria=list(item["criteria"]),
                )
                for item in payload["obligations"]
            ],
        )

    def run_checkpoint(
        self, *, destination: Path, candidate: str, omit_path: str | None = None
    ) -> CheckpointObservation:
        """Apply a candidate to a fresh clone and run the authoritative loop."""

        self._checkpoints += 1
        label = f"{candidate}{'-omit-' + Path(omit_path).name if omit_path else ''}"
        record_dir = self.evidence_root / f"checkpoint-{self._checkpoints:02d}-{label}"
        record_dir.mkdir(parents=True, exist_ok=True)

        with tempfile.TemporaryDirectory() as scratch:
            workspace = Path(scratch)
            base = workspace / "base"
            candidate_tree = workspace / "candidate"
            # Two independent clones. Copying the base into the candidate would
            # be faster and would also mean a defect in the copy shows up as a
            # delta the checkpoint attributes to the model.
            self._observer.clone_seed(destination=base)
            self._observer.clone_seed(destination=candidate_tree)
            shutil.rmtree(base / ".git")
            shutil.rmtree(candidate_tree / ".git")

            for relative, body in self._candidate_files(candidate).items():
                if omit_path is not None and relative == omit_path:
                    continue
                target = candidate_tree / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(body)

            artifact = record_dir / "coverage.json"
            emitted: list[str] = []

            def emit(snapshot: Path, fingerprint: str):
                def runner(argv, *, timeout_seconds):
                    completed = subprocess.run(  # noqa: S603
                        argv,
                        cwd=snapshot,
                        capture_output=True,
                        text=True,
                        env=_clean_environment(),
                        timeout=timeout_seconds,
                    )
                    return completed.returncode, completed.stdout, completed.stderr

                witness = emit_test_witness(
                    runner,
                    command_name="unit-tests",
                    command_version="1",
                    argv=[self.python, str(self._runner_script), str(artifact)],
                    worktree_fingerprint=fingerprint,
                    coverage_artifact=artifact,
                    criteria_proved=self._acceptance_claimed_criteria(),
                    collection_method="stdlib trace module",
                )
                emitted.append(witness.witness_id)
                return [witness]

            record = run_checkpoint(
                self._contract(),
                base_root=base,
                candidate_root=candidate_tree,
                snapshot_root=workspace / "snapshot",
                emit_witnesses=emit,
            )

        return self._observe(record, record_dir)

    def run_checkpoint_on_worktree(
        self, *, worktree: Path, label: str
    ) -> CheckpointObservation:
        """Score a worktree the agent produced, rather than a packaged candidate.

        `run_checkpoint` exists to qualify the *package*: it applies a named
        candidate from the package to a fresh clone, which is exactly right for
        proving the case detects what it claims to. It is the wrong instrument
        for a slot, because the thing under test there is what Qwen wrote, and
        a packaged candidate would score the package again while looking like
        it had scored the agent.

        The base clone is still taken fresh from the seed, so the diff is
        against the same starting point every slot began from.
        """

        self._checkpoints += 1
        record_dir = self.evidence_root / f"checkpoint-{self._checkpoints:02d}-{label}"
        record_dir.mkdir(parents=True, exist_ok=True)

        with tempfile.TemporaryDirectory() as scratch:
            workspace = Path(scratch)
            base = workspace / "base"
            candidate_tree = workspace / "candidate"

            self._observer.clone_seed(destination=base)
            shutil.rmtree(base / ".git")

            # A copy, so the checkpoint cannot mutate the evidence it is
            # reading, and without `.git`, which would otherwise show up as a
            # difference the checkpoint attributes to the agent.
            shutil.copytree(worktree, candidate_tree)
            git_dir = candidate_tree / ".git"
            if git_dir.exists():
                shutil.rmtree(git_dir)

            artifact = record_dir / "coverage.json"
            emitted: list[str] = []

            def emit(snapshot: Path, fingerprint: str):
                def runner(argv, *, timeout_seconds):
                    completed = subprocess.run(  # noqa: S603
                        argv,
                        cwd=snapshot,
                        capture_output=True,
                        text=True,
                        env=_clean_environment(),
                        timeout=timeout_seconds,
                    )
                    return completed.returncode, completed.stdout, completed.stderr

                witness = emit_test_witness(
                    runner,
                    command_name="unit-tests",
                    command_version="1",
                    argv=[self.python, str(self._runner_script), str(artifact)],
                    worktree_fingerprint=fingerprint,
                    coverage_artifact=artifact,
                    criteria_proved=self._acceptance_claimed_criteria(),
                    collection_method="stdlib trace module",
                )
                emitted.append(witness.witness_id)
                return [witness]

            record = run_checkpoint(
                self._contract(),
                base_root=base,
                candidate_root=candidate_tree,
                snapshot_root=workspace / "snapshot",
                emit_witnesses=emit,
            )

        return self._observe(record, record_dir)

    def _observe(self, record, record_dir: Path) -> CheckpointObservation:
        """Translate one `CheckpointRecord` into what the proofs ask about.

        A translation, not a judgement. Everything below is read off the record
        the authoritative loop produced; nothing is recomputed, because a
        second opinion here would be a second thing to be wrong.
        """

        contract = self._contract()
        criteria_of = {
            obligation.obligation_id: tuple(obligation.criteria)
            for obligation in contract.obligations
        }

        satisfied: list[str] = []
        blocks: tuple[str, ...] = ()
        commands: tuple[CommandOutcome, ...] = ()
        readiness = record.readiness
        if readiness is not None:
            for result in readiness.obligations:
                if result.status is ObligationStatus.PROVED:
                    satisfied.extend(criteria_of.get(result.obligation_id, ()))
            # The changed-behaviour criterion is not an obligation; it is the
            # readiness report's own statement that every addition is reached.
            if not readiness.unexercised_behaviour:
                satisfied.append("AC-CHANGED-BEHAVIOUR-EXERCISED")
            blocks = tuple(
                sorted({str(item.block).upper() for item in readiness.findings})
            )
            commands = tuple(
                CommandOutcome(
                    name="unit-tests",
                    exit_code=0,
                    worktree_fingerprint=record.candidate_fingerprint,
                )
                for _ in record.witness_ids
            )

        observation = CheckpointObservation(
            outcome=str(record.decision.outcome).upper().rsplit(".", 1)[-1],
            satisfied_criteria=tuple(sorted(set(satisfied))),
            readiness_blocks=blocks,
            repair_packet=record.decision.repair_packet or "",
            commands=commands,
            emitter_failed=record.emitter_error is not None,
        )
        (record_dir / "checkpoint-record.json").write_text(
            json.dumps(record.model_dump(mode="json"), indent=2, sort_keys=True)
            + "\n",
            encoding="utf-8",
        )
        (record_dir / "observation.json").write_text(
            json.dumps(observation.model_dump(mode="json"), indent=2, sort_keys=True)
            + "\n",
            encoding="utf-8",
        )
        return observation
