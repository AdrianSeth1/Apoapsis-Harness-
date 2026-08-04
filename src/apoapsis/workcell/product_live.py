"""Linux-side controller for one ordinary Capability Sandbox plan slice."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from contextlib import ExitStack, contextmanager
from pathlib import Path

from apoapsis.architect.schema import ArchitecturePlan
from apoapsis.qualification.fake_pilot_provider import ScriptId
from apoapsis.qualification.fake_provider_server import FakeProviderServer
from apoapsis.qualification.live_pilot import (
    ModelServer,
    _prepare_containment_workspace,
    verify_runtime,
)
from apoapsis.qualification.pilot import PilotManifest
from apoapsis.qualification.rehearsal import EvidenceWriter, StageOutcome
from apoapsis.qualification.runner import stage_1_runtime_identity, stage_2_containment
from apoapsis.qualification.session_factory import session_factory_from_manifest
from apoapsis.qualification.slot_driver import controller_address, execute_slot
from apoapsis.workcell.acceptance import CheckpointOutcome
from apoapsis.workcell.admission import AdmissionPolicy
from apoapsis.workcell.checkpoint import CheckpointRecord, run_checkpoint
from apoapsis.workcell.contract_compiler import compile_slice_contract
from apoapsis.workcell.emitters import emit_test_witness
from apoapsis.workcell.parity import evaluate_parity
from apoapsis.workcell.progress import PROGRESS_FILENAME, ProgressJournal, RunStage
from apoapsis.workcell.server_lease import ModelServerLease


_TRACE_RUNNER = r'''\
import json, os, sys, trace, unittest
artifact = sys.argv[1]
arguments = sys.argv[2:]
root = os.getcwd()
if root not in sys.path: sys.path.insert(0, root)
tracer = trace.Trace(count=1, trace=0, ignoredirs=[sys.prefix, sys.exec_prefix])
box = {}
def run():
    old = sys.argv
    try:
        sys.argv = ["python -m unittest", *arguments]
        box["program"] = unittest.main(module=None, exit=False)
    finally:
        sys.argv = old
tracer.runfunc(run)
files = {}
for (filename, lineno), hits in tracer.results().counts.items():
    if not hits or filename.startswith("<") or not os.path.isfile(filename): continue
    relative = os.path.relpath(os.path.realpath(filename), root)
    if relative.startswith(os.pardir) or os.path.isabs(relative): continue
    files.setdefault(relative.replace(os.sep, "/"), set()).add(lineno)
with open(artifact, "w", encoding="utf-8") as handle:
    json.dump({"files": {p: {"executed_lines": sorted(v)} for p, v in sorted(files.items())}}, handle)
result = box["program"].result
sys.exit(0 if result.wasSuccessful() else 1)
'''


def _windows_path(path: Path) -> str:
    text = str(path.resolve())
    if text.startswith("/mnt/") and len(text) > 7:
        drive = text[5].upper()
        return drive + ":\\" + text[7:].replace("/", "\\")
    return text


def _task_text(request: dict) -> str:
    task = request["task"]
    plan = request["plan"]
    target = next(item for item in plan["slices"] if item["slice_id"] == request["slice_id"])
    # The verbatim text is the shorthand the user typed; the interpreted
    # meaning is what the architect derived from it and what the constraint
    # actually requires. Sending only the first told a model building for a
    # Windows workstation nothing but "Runs on an RTX 4090." -- so it wrote
    # POSIX-only paths into its tests, which passed in this Linux container
    # and failed the moment independent verification ran them on the host.
    constraints = "\n".join(
        f"- {item['id']}: {item['text']}"
        + (
            f"\n  Meaning: {item['interpreted_meaning']}"
            if item.get("interpreted_meaning")
            else ""
        )
        for item in task.get("hard_constraints", [])
    )
    criteria = "\n".join(
        f"- {item['id']}: {item['text']}" for item in task.get("acceptance_criteria", [])
    )
    return (
        f"# Approved plan slice {request['slice_id']}\n\n"
        f"Objective: {target['objective']}\n\n"
        f"Work brief: {target['work_brief']}\n\n"
        f"Implementation steps:\n" + "\n".join(f"- {x}" for x in target["implementation_steps"]) + "\n\n"
        f"Suggested paths (advisory):\n" + "\n".join(f"- {x}" for x in target["suggested_paths"]) + "\n\n"
        f"Test obligations:\n" + "\n".join(f"- {x}" for x in target["test_obligations"]) + "\n\n"
        f"Hard constraints:\n{constraints}\n\nAcceptance criteria:\n{criteria}\n\n"
        + _verification_environment_text(request)
        # Before the contract, and before the instruction to implement: what
        # already exists is the thing a fresh session would otherwise spend its
        # first turns discovering.
        + str(request.get("orientation") or "")
        + _judgement_contract(request)
        + "Implement only this slice. Run the relevant tests before declaring readiness."
    )


#: Rough characters-per-token. The exact number does not matter; the ceiling
#: existing does. Both constants are public so the deterministic test can
#: enforce the bound -- a budget nothing checks is a comment, and this text is
#: sent on every turn of every slice.
CHARS_PER_TOKEN_ESTIMATE = 4

#: The longest the judgement contract may get (ADR 0103). It is a contract
#: statement, not a rule wall: past a few hundred tokens, long instruction
#: blocks measurably degrade a small model's compliance with the rules that are
#: *not* enforced mechanically, which is the opposite of what this is for.
MAX_JUDGEMENT_CONTRACT_TOKENS = 250


def _judgement_contract(request: dict) -> str:
    """State how completion is actually decided, before the model guesses.

    Live evidence (CAP-4EE9F101146E4556's stream log) shows Qwen spending
    hundreds of lines of thinking reverse-engineering the witness system from
    repair-packet error strings -- "maybe it parses output for markers... let me
    invent `AC-007 PASS / EXERCISED backend/app.py sha256:...`... wait, maybe
    it's a coverage tool" -- and then implementing marker schemes that do
    nothing. The packet told it *what* was unproved and never *how* proof is
    established, so the speculation was the only rational move available.

    This says how. It is deliberately mechanical and free of internal
    vocabulary: no witness, obligation, or behaviour unit appears, because a
    model that has to learn our nouns to comply is being taxed for our
    convenience. Same text in the initial task and in every repair packet, from
    one constant, so the two can never drift into telling different stories.
    """

    policy = request.get("patch_policy") or {}
    commands = ", ".join(
        f"`{' '.join(item['argv'])}`"
        for item in request.get("verification_commands", [])
    ) or "the approved verification commands"
    limits = []
    if policy.get("max_files"):
        limits.append(f"at most {policy['max_files']} changed files")
    if policy.get("max_changed_lines"):
        limits.append(f"at most {policy['max_changed_lines']} changed lines")
    limits.append(
        "test files may be added or changed"
        if policy.get("allow_test_changes", True)
        else "test files must not change"
    )
    limits.append(
        "dependency manifests may change"
        if policy.get("allow_dependency_changes", True)
        else "dependency manifests must not change"
    )
    return (
        "How this slice is judged\n\n"
        "Nothing you print is proof. When you stop, Apoapsis, outside this "
        "container:\n"
        "1. Snapshots your files and compares them with the approved base by "
        "content. Git history is ignored, so commits and resets change "
        "nothing.\n"
        f"2. Judges the change as one unit -- {'; '.join(limits)}. Exceeding "
        "one refuses all of it.\n"
        f"3. Re-runs {commands} from that snapshot, recording which lines of "
        "your code execute.\n"
        "4. Counts an acceptance criterion met only if those commands pass.\n"
        "5. Requires every production file, function or class you add to have "
        "at least one line execute in that run. Inherited tests passing is no "
        "evidence for new code: they pass by never reaching it.\n\n"
        "So: for everything you add, add or extend a test that calls it. Do "
        "not print markers, hashes or coverage summaries; execution is "
        "observed directly; your output is not read as evidence.\n\n"
    )


def _verification_environment_text(request: dict) -> str:
    """State where the deciding verification actually runs.

    The model works in this Linux container and its own test runs are green
    here, but the verdict that decides the slice comes from independent
    verification on the operator's host. When those differ, every signal the
    model can see says it succeeded, and it is told afterwards that it
    failed -- with no way to have known. Naming the host platform is the
    difference between an unfair test and a solvable one.
    """

    environment = request.get("independent_verification") or {}
    platform_name = environment.get("platform")
    if not platform_name:
        return ""
    portability = ""
    if platform_name != "Linux":
        portability = (
            f" Code and tests must therefore pass on {platform_name} as well as "
            "here. Do not hardcode POSIX-only paths such as '/tmp/...': on "
            f"{platform_name} they are not absolute and will be rejected. Use "
            "`tempfile` and `pathlib` so paths are correct on both."
        )
    return (
        "Verification environment: you are working inside a Linux container, "
        f"but this slice is decided by verification run on {platform_name}."
        f"{portability}\n\n"
    )


def _base_tree(seed: Path, target: Path) -> None:
    shutil.copytree(seed, target)
    # The controller runs as root while a bind-mounted Windows/WSL seed may
    # retain UID 1000 ownership. Trust only this exact disposable copy for each
    # command; never mutate global/system Git configuration or broaden trust to
    # the mount, repository parent, or wildcard (ADR 0099).
    safe_directory = f"safe.directory={target}"
    subprocess.run(
        ["git", "-c", safe_directory, "clean", "-xdff", "--quiet"],
        cwd=target,
        check=True,
    )
    subprocess.run(
        ["git", "-c", safe_directory, "reset", "--hard", "--quiet", "HEAD"],
        cwd=target,
        check=True,
    )
    shutil.rmtree(target / ".git")


def _live_preflight(
    manifest: PilotManifest, *, repo: Path, seed: Path, evidence: Path,
    runtime_root: Path, task_text: str
) -> dict:
    """Reobserve the manifest-declared tool and containment gates before inference."""

    scratch = runtime_root / "scratch"
    scratch.mkdir(parents=True, exist_ok=False)
    writer = EvidenceWriter(evidence / "observations")
    identity = stage_1_runtime_identity(
        manifest,
        repo=repo,
        seed_repository=seed,
        scratch=scratch,
        writer=writer,
    )
    provider = FakeProviderServer(
        ScriptId.COMPLETE_PROPOSAL,
        model_name=manifest.model.model_alias,
        host="0.0.0.0",
        transcript_path=evidence / "containment-provider.json",
    )
    provider.start()
    try:
        port = provider.base_url.rsplit(":", 1)[1]
        workspace = scratch / "containment-workspace"
        _prepare_containment_workspace(workspace)
        forwarder = scratch / "forwarder.py"
        task = scratch / "task.md"
        shutil.copyfile(repo / "src/apoapsis/workcell/forwarder.py", forwarder)
        task.write_text(task_text, encoding="utf-8")
        session = session_factory_from_manifest(
            manifest,
            repo=repo,
            workspace=workspace,
            socket_directory=scratch / "containment-sockets",
            upstream_base_url=f"http://{controller_address()}:{port}",
            forwarder_path=forwarder,
            task_artifact_path=task,
        )
        with session:
            exit_code, stderr = session.start_forwarder()
            if exit_code != 0:
                raise RuntimeError(f"preflight forwarder failed: {stderr}")
            containment = stage_2_containment(
                manifest,
                session=session,
                repo=repo,
                seed_repository=seed,
                scratch=scratch,
                writer=writer,
            )
    finally:
        provider.stop()
    gates = {
        "runtime_identity": identity.model_dump(mode="json"),
        "containment": containment.model_dump(mode="json"),
    }
    (evidence / "gates.json").write_text(
        json.dumps(gates, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    if identity.outcome != StageOutcome.PASSED or containment.outcome != StageOutcome.PASSED:
        raise RuntimeError(
            f"live preflight refused: identity={identity.outcome.value}, "
            f"containment={containment.outcome.value}"
        )
    return gates


class ProductSupervisor:
    def __init__(
        self,
        request: dict,
        base: Path,
        evidence: Path,
        journal: ProgressJournal | None = None,
    ) -> None:
        self.request = request
        self.base = base
        self.evidence = evidence
        # Optional so every existing construction (tests, the control arm
        # built before a journal exists) keeps working unchanged. A supervisor
        # with no journal simply records nothing, which is the pre-MH-9
        # behaviour rather than a degraded one.
        self.journal = journal
        self.evidence.mkdir(parents=True, exist_ok=True)
        self.contract = compile_slice_contract(
            ArchitecturePlan.model_validate(request["plan"]), request["slice_id"]
        )
        self.commands = {item["name"]: item for item in request["verification_commands"]}
        self.records: list[CheckpointRecord] = []
        self.final_snapshot: Path | None = None
        self.trace_runner = evidence / "unittest_trace_runner.py"
        self.trace_runner.write_text(_TRACE_RUNNER, encoding="utf-8")

    def checkpoint(self, candidate: Path, turn: int) -> CheckpointRecord:
        record_dir = self.evidence / f"checkpoint-{turn + 1:02d}"
        record_dir.mkdir(parents=True, exist_ok=True)
        snapshot = record_dir / "admitted-snapshot"

        def emit(admitted: Path, fingerprint: str):
            witnesses = []
            for name in self.contract.required_commands:
                command = self.commands.get(name)
                if command is None:
                    raise RuntimeError(f"approved verification command {name!r} is missing")
                argv = list(command["argv"])
                try:
                    module = argv.index("-m")
                except ValueError as exc:
                    raise RuntimeError(
                        f"{name!r} has no controller witness adapter; human review required"
                    ) from exc
                if module + 1 >= len(argv) or argv[module + 1] != "unittest":
                    raise RuntimeError(
                        f"{name!r} is not a unittest command and has no structured witness adapter"
                    )
                artifact = record_dir / f"{name}-coverage.json"
                # `sys.executable`, never a bare "python". This runs inside
                # the controller container, whose base installs python3 and
                # python3-venv and provides no `python` alias at all, so a
                # literal "python" raises FileNotFoundError before the trace
                # runner starts. The witness is then never emitted -- and a
                # candidate with no witness is reported as "no current-state
                # witness proves this file is reached", which reads as the
                # coding model failing to test its own code rather than as
                # the controller being unable to run the command. `python3`
                # would resolve, but only `sys.executable` is guaranteed to
                # be the interpreter that already has this package importable.
                traced_argv = [
                    sys.executable,
                    str(self.trace_runner),
                    str(artifact),
                    *argv[module + 2:],
                ]

                def runner(run_argv, *, timeout_seconds):
                    env = {
                        k: v for k, v in os.environ.items()
                        if k.lower() not in {"http_proxy", "https_proxy", "all_proxy", "ftp_proxy"}
                    }
                    env.update({"PYTHONDONTWRITEBYTECODE": "1", "NO_PROXY": "*"})
                    done = subprocess.run(
                        run_argv, cwd=admitted, capture_output=True, text=True,
                        env=env, timeout=timeout_seconds,
                    )
                    return done.returncode, done.stdout, done.stderr

                witnesses.append(emit_test_witness(
                    runner,
                    command_name=name,
                    command_version="1",
                    argv=traced_argv,
                    worktree_fingerprint=fingerprint,
                    coverage_artifact=artifact,
                    criteria_proved=list(self.contract.criteria),
                    timeout_seconds=float(command["timeout_seconds"]),
                    collection_method="stdlib trace module",
                    # The tree the command ran against, so coverage can be
                    # read against the source and an imported-but-never-called
                    # module is not mistaken for an exercised one.
                    source_root=Path(admitted),
                ))
            return witnesses

        patch = self.request["patch_policy"]
        record = run_checkpoint(
            self.contract,
            base_root=self.base,
            candidate_root=candidate,
            snapshot_root=snapshot,
            emit_witnesses=emit,
            policy=AdmissionPolicy(
                max_files=patch["max_files"],
                max_changed_lines=patch["max_changed_lines"],
                allow_test_changes=patch["allow_test_changes"],
                allow_dependency_changes=patch["allow_dependency_changes"],
            ),
        )
        (record_dir / "checkpoint.json").write_text(
            json.dumps(record.model_dump(mode="json"), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        self.records.append(record)
        if self.journal is not None:
            obligations = list(record.readiness.obligations) if record.readiness else []
            self.journal.checkpoint_verdict(
                attempt=turn + 1,
                outcome=record.decision.outcome.value,
                detail=record.decision.detail,
                operator=(
                    record.decision.operator.model_dump(mode="json")
                    if record.decision.operator is not None
                    else None
                ),
                obligations_proved=sum(
                    1 for item in obligations if item.status.value == "proved"
                ),
                obligations_total=len(obligations),
            )
        if record.decision.outcome == CheckpointOutcome.COMPLETE:
            self.final_snapshot = snapshot
        return record

    def __call__(self, candidate: Path, turn: int):
        record = self.checkpoint(candidate, turn)
        if record.decision.outcome in {
            CheckpointOutcome.COMPLETE, CheckpointOutcome.HUMAN_REVIEW_REQUIRED
        }:
            return record, None
        return record, (
            "Apoapsis inspected the complete candidate and did not accept it. "
            "Repair every item below, rerun the relevant tests, then finish the slice.\n\n"
            # The same contract as the initial task, restated here rather than
            # assumed remembered: this prompt continues a session whose context
            # may already have been compressed, and a repair packet that names
            # what is unproved without saying how proof works is exactly what
            # sent the model off inventing marker formats.
            + _judgement_contract(self.request)
            + record.decision.repair_packet
        )


def _http_transport(path: str, method: str = "GET", body: dict | None = None):
    """Talk to the loopback model server. The lease's only I/O."""

    import urllib.error
    import urllib.request

    url = f"http://127.0.0.1:8080{path}"
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"} if data else {},
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            raw = response.read()
            try:
                payload = json.loads(raw) if raw else {}
            except json.JSONDecodeError:
                payload = {}
            return response.status, payload if isinstance(payload, dict) else {}
    except urllib.error.HTTPError as exc:
        return exc.code, {}


@contextmanager
def _arm_server(lease, arm: str, evidence: Path, manifest):
    """The server for one arm: the lease's, or a cold start if it cannot vouch.

    The fallback is the whole safety argument. A lease that verifies is a
    saved 16.8 GB load; a lease that cannot verify falls back to exactly the
    behaviour that existed before it, so this is never worse than reloading --
    only sometimes much faster. What is never allowed is the third option:
    serving an arm from a server whose identity nobody could establish.
    """

    verification = lease.verify(arm)
    if verification.verified:
        lease.reset_slots(arm)
        yield "leased"
        return
    evidence.mkdir(parents=True, exist_ok=True)
    (evidence / "server-lease-fallback.json").write_text(
        json.dumps(verification.model_dump(mode="json"), indent=2, sort_keys=True)
        + "\n",
        encoding="utf-8",
    )
    with ModelServer(manifest, evidence / "server") as server:
        server.readiness()
        yield "cold_start"



def run(
    request_path: Path,
    response_path: Path,
    repo: Path,
    seed: Path,
    runtime_root: Path,
    *,
    containment_preflight_only: bool = False,
) -> int:
    request = json.loads(request_path.read_text(encoding="utf-8"))
    manifest = PilotManifest.model_validate_json(
        (repo / "docs/qualification/slice7-crisis-atlas-pilot-manifest-v8.json").read_text(encoding="utf-8")
    )
    if request.get("runtime_profile") != "crisis-atlas-v8-qwen3.6-27b":
        raise RuntimeError("the authorized Capability Sandbox runtime profile is unknown")
    if request.get("qualified_model_alias") != manifest.model.model_alias:
        raise RuntimeError("the authorized model alias differs from the pinned manifest")
    run_root = response_path.parent / "controller-runtime"
    run_root.mkdir(parents=True, exist_ok=False)
    base = run_root / "approved-base"
    _base_tree(seed, base)
    evidence = response_path.parent / "evidence"
    evidence.mkdir(parents=True, exist_ok=True)
    # Unix sockets and sibling-container workspaces must live on the WSL host's
    # ext4 filesystem, not in the Windows audit directory. The launcher mounts
    # this fresh runtime path into the controller at the identical absolute
    # path so the host Docker daemon can resolve sibling bind sources.
    runtime_root.mkdir(parents=True, exist_ok=False)
    # Opened before the first stage so a run that dies inside preflight still
    # leaves a journal saying it got that far. The file lives beside the other
    # evidence so the status projection finds it by convention (MH-9).
    journal = ProgressJournal(evidence / PROGRESS_FILENAME)
    journal.started(
        run_id=request.get("run_id"),
        slice_id=request.get("slice_id"),
        context_window_tokens=manifest.budgets.context_limit_tokens,
        parity_arm_expected=bool(request.get("high_assurance_parity_guard")),
    )
    with journal.stage(RunStage.PREFLIGHT):
        runtime = verify_runtime(manifest)
        (evidence / "runtime-preflight.json").write_text(
            json.dumps(runtime, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        task_text = _task_text(request)
        gates = _live_preflight(
            manifest,
            repo=repo,
            seed=seed,
            evidence=evidence / "live-preflight",
            runtime_root=runtime_root / "p",
            task_text=task_text,
        )
    if containment_preflight_only:
        journal.finished(outcome="containment_preflight_complete")
        response_path.write_text(
            json.dumps(
                {
                    "schema_version": "1.0",
                    "outcome": "containment_preflight_complete",
                    "gates": gates,
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        return 0
    # One server for this run, verified before each arm instead of reloaded.
    # `ModelServer`'s own cold-start path is still exactly what happens when a
    # verification fails, so the worst case here is the previous behaviour.
    lease = ModelServerLease(
        manifest,
        evidence / "model-server",
        server_factory=lambda directory: ModelServer(manifest, directory),
        transport=_http_transport,
    )
    # The 16.8 GB load is the single longest opaque wait in a run (ADR 0107
    # made it happen once per run instead of once per arm; this makes it
    # visible). `ExitStack` rather than a nested `with` so the lease is still
    # released by a context manager with real exception information, while the
    # load itself sits inside its own timed stage.
    with ExitStack() as stack:
        with journal.stage(RunStage.MODEL_LOADING):
            stack.enter_context(lease)
        return _run_arms(
            request=request,
            manifest=manifest,
            repo=repo,
            seed=seed,
            base=base,
            evidence=evidence,
            runtime_root=runtime_root,
            response_path=response_path,
            task_text=task_text,
            lease=lease,
            journal=journal,
        )


def _run_arms(
    *,
    request: dict,
    manifest,
    repo: Path,
    seed: Path,
    base: Path,
    evidence: Path,
    runtime_root: Path,
    response_path: Path,
    task_text: str,
    lease: ModelServerLease,
    journal: ProgressJournal | None = None,
) -> int:
    """Both arms of one slice, against the leased server.

    Split out of `run` only so the lease's lifetime is a `with` block rather
    than a `try/finally` wrapped around three hundred lines: the server must be
    released whichever way the slice ends, and the shape should make that
    obvious rather than careful.
    """

    journal = journal or ProgressJournal(evidence / PROGRESS_FILENAME)
    control_record = None
    control_observation = None
    if bool(request.get("high_assurance_parity_guard")):
        control_supervisor = ProductSupervisor(request, base, evidence / "control")
        with journal.stage(RunStage.CONTROL_ARM), _arm_server(
            lease, "default-qwen-control", evidence / "control", manifest
        ):
            control_observation = execute_slot(
                manifest,
                repo=repo,
                seed_repository=seed,
                base=runtime_root / "s",
                repetition_id=request["run_id"],
                arm="default-qwen-control",
                script=None,
                evidence_dir=evidence / "control",
                max_output_tokens=manifest.budgets.max_output_tokens,
                qwen_timeout_seconds=manifest.budgets.per_arm_wall_clock_seconds,
                keep_workspace=True,
                live_upstream_base_url="http://127.0.0.1:8080",
                stream_json=True,
                task_text_override=task_text,
                write_workspace_task=False,
                inject_stream_usage_options=True,
            )
        if control_observation.error is None and control_observation.kept_workspace:
            control_record = control_supervisor.checkpoint(
                control_observation.kept_workspace, 0
            )

    # The sandbox arm is the one that ships work, so it is the arm whose
    # checkpoints reach the journal. The control arm's verdicts are recorded
    # under `parity_guard` in the result and would only make the status view
    # ambiguous about which arm an operator is watching.
    supervisor = ProductSupervisor(request, base, evidence / "sandbox", journal)

    def observe_usage(index: int, record) -> None:
        """One exchange, journaled as the relay finishes recording it."""

        journal.model_call(
            call=index,
            input_tokens=getattr(record, "input_tokens", None),
            output_tokens=getattr(record, "output_tokens", None),
            cached_input_tokens=getattr(
                getattr(record, "usage", None), "cached_input_tokens", None
            ),
            arm="apoapsis-sandbox",
        )

    with journal.stage(RunStage.MODEL_RUNNING), _arm_server(
        lease, "apoapsis-sandbox", evidence / "sandbox", manifest
    ):
        observation = execute_slot(
            manifest,
            repo=repo,
            seed_repository=seed,
            base=runtime_root / "s",
            repetition_id=request["run_id"],
            arm="apoapsis-sandbox",
            script=None,
            evidence_dir=evidence / "sandbox",
            max_output_tokens=manifest.budgets.max_output_tokens,
            qwen_timeout_seconds=manifest.budgets.per_arm_wall_clock_seconds,
            keep_workspace=True,
            live_upstream_base_url="http://127.0.0.1:8080",
            supervisor=supervisor,
            continuation_limit=int(request["max_native_continuations"]),
            stream_json=True,
            task_text_override=task_text,
            write_workspace_task=False,
            inject_stream_usage_options=True,
            usage_observer=observe_usage,
        )
    final = supervisor.records[-1] if supervisor.records else None

    def proved(record: CheckpointRecord | None) -> int | None:
        if record is None:
            return None
        return sum(
            1 for item in (record.readiness.obligations if record.readiness else [])
            if item.status.value == "proved"
        )

    parity = evaluate_parity(
        expected=bool(request.get("high_assurance_parity_guard")),
        control_proved=proved(control_record),
        candidate_proved=proved(final),
    )
    parity_unavailable = parity.unavailable
    parity_regression = parity.regression
    outcome = "complete" if (
        observation.error is None
        and not parity_unavailable
        and not parity_regression
        and final is not None
        and final.decision.outcome == CheckpointOutcome.COMPLETE
        and supervisor.final_snapshot is not None
    ) else "human_review_required"
    detail = observation.error or (
        "the high-assurance matched control produced no scoreable checkpoint"
        if parity_unavailable else
        "the supervised candidate was inferior to the matched default-Qwen control"
        if parity_regression else
        final.decision.detail if final is not None else "the native agent produced no checkpoint"
    )
    # Written next to the arm's other evidence rather than only summed into the
    # result: a total answers "what did this cost", and only the series answers
    # "did context grow across the slice", which is the question the sandbox
    # path could not previously answer at all.
    (evidence / "sandbox" / "model-usage-series.json").write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "run_id": request["run_id"],
                "arm": "apoapsis-sandbox",
                "calls": [
                    {
                        "call": index,
                        "input_tokens": input_tokens,
                        "output_tokens": output_tokens,
                    }
                    for index, input_tokens, output_tokens in observation.usage_series
                ],
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    payload = {
        "schema_version": "1.0",
        "outcome": outcome,
        "detail": detail,
        # The sandbox arm only. The control arm's cost is reported under
        # `parity_guard`, because merging the two would report a slice as
        # costing twice what the delivered work cost.
        "model_usage": {
            "calls": observation.calls_with_usage,
            "input_tokens": observation.input_tokens,
            "output_tokens": observation.output_tokens,
            "cached_input_tokens": observation.cached_input_tokens,
            "peak_input_tokens": observation.peak_input_tokens,
            "series": [
                {
                    "call": index,
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                }
                for index, input_tokens, output_tokens in observation.usage_series
            ],
        },
        "turns": len(supervisor.records),
        "base_path_windows": _windows_path(base),
        "snapshot_path_windows": (
            _windows_path(supervisor.final_snapshot)
            if supervisor.final_snapshot is not None else None
        ),
        "checkpoint": final.model_dump(mode="json") if final is not None else None,
        "relay_requests": observation.provider_requests,
        "incomplete_relay_responses": list(observation.incomplete_relay_responses),
        # How many times the weights were actually loaded for this slice, and
        # what was checked before each arm reused them. Reported so the saving
        # is a number rather than an impression, and so a fallback is visible.
        "model_server_lease": lease.record.model_dump(mode="json"),
        "parity_guard": {
            "enabled": bool(request.get("high_assurance_parity_guard")),
            # Why this slice did or did not pair, recorded rather than left
            # to be re-derived. "No control arm ran" is not evidence of
            # anything; "no control arm ran because this is slice 3 and the
            # policy pairs the first and every 4th" is (ADR 0108).
            "selection": request.get("parity_selection"),
            "regression": parity_regression,
            "unavailable": parity_unavailable,
            "control_checkpoint": (
                control_record.model_dump(mode="json") if control_record is not None else None
            ),
            "control_error": (
                control_observation.error if control_observation is not None else None
            ),
            "control_model_usage": (
                {
                    "calls": control_observation.calls_with_usage,
                    "input_tokens": control_observation.input_tokens,
                    "output_tokens": control_observation.output_tokens,
                }
                if control_observation is not None
                else None
            ),
        },
    }
    response_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    # Last, after the result exists. A journal that said "finished" before the
    # result was on disk would let a status view report a completed run whose
    # result a reader then could not find.
    journal.finished(outcome=outcome, detail=detail)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--request", type=Path, required=True)
    parser.add_argument("--response", type=Path, required=True)
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--seed", type=Path, required=True)
    parser.add_argument("--runtime-root", type=Path, required=True)
    parser.add_argument("--containment-preflight-only", action="store_true")
    args = parser.parse_args()
    try:
        return run(
            args.request,
            args.response,
            args.repo,
            args.seed,
            args.runtime_root,
            containment_preflight_only=args.containment_preflight_only,
        )
    finally:
        # Inner workcells intentionally run under a non-root UID and may leave
        # directories the WSL launcher user cannot remove. The root controller
        # owns only this freshly created runtime subtree and removes it before
        # the launcher's exact mktemp-root trap handles the normalized seed.
        shutil.rmtree(args.runtime_root, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
