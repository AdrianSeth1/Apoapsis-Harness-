"""Slice 5C live qualification, run inside the controller container.

Option B (ADR 0081): Qwen's native loop is authoritative, Apoapsis speaks only
between invocations. This script proves -- or fails to prove -- that the
handoff actually works against the real thing.

Everything runs through `LiveWorkcellSession`, so the workcell stays at
`--network none` and the only route to the model is
loopback -> in-container forwarder -> Unix socket -> controller-owned relay.

Two containment assertions are enforced here rather than assumed:

* the workcell must NOT be able to reach the upstream directly, and
* a successful model turn must produce relay traffic. Zero relay requests
  alongside a working turn would mean the model was reached by some path the
  controller does not mediate, which is worse than a failure.

Stages, in order, each writing its own raw evidence file. A stage that cannot
run marks its verdict and does not fabricate a downstream one.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path

from apoapsis.workcell.clone import create_sanitized_clone
from apoapsis.workcell.containment import (
    DEFAULT_CONTAINMENT_PROBES,
    classify_probe,
    evaluate_containment,
)
from apoapsis.workcell.live_session import LiveWorkcellSession
from apoapsis.workcell.pins import WorkcellConfig

HOST_ROOT = Path(
    os.environ.get(
        "SLICE5C_ROOT", "/mnt/docker-desktop-disk/data/apoapsis-slice5c-2026-07-30"
    )
)
SOURCE_REPO = Path("/src-repo")
UPSTREAM = os.environ.get("UPSTREAM", "http://host.docker.internal:8080")
EV = HOST_ROOT / "evidence"

CLI_HOME = "/tmp/qwen-home"
WORKDIR = "/workspace"

NATIVE_EDIT = {"write_file", "replace", "edit"}
NATIVE_SHELL = {"run_shell_command", "shell", "bash"}

TASK_TEXT = """# Task

Work in the checked-out project. Ask for a checkpoint when you believe the
slice is done; a checkpoint is an inspection, not a completion.
"""


def write(name: str, payload) -> None:
    EV.mkdir(parents=True, exist_ok=True)
    target = EV / name
    text = payload if isinstance(payload, str) else json.dumps(payload, indent=2)
    target.write_text(text[:8_000_000], encoding="utf-8")


class Qwen:
    """One native invocation per call. Qwen owns its loop; we observe it.

    The same container, the same HOME and the same working directory across
    every call -- that identity is what makes `--resume` meaningful, so it is
    fixed here rather than passed in per call.
    """

    def __init__(self, session: LiveWorkcellSession) -> None:
        self.session = session
        self.calls: list[dict] = []

    def __call__(
        self, prompt: str, *, resume: str | None = None, timeout: float = 1500.0
    ) -> dict:
        flags = f"--resume {shlex.quote(resume)} " if resume else ""
        before = self.session.relay_request_count()
        started = time.monotonic()
        code, stdout, stderr = self.session.exec(
            [
                "sh",
                "-c",
                f"cd {WORKDIR} && HOME={CLI_HOME} timeout {int(timeout)} "
                f"qwen -o stream-json {flags}-p {shlex.quote(prompt)}",
            ],
            timeout_seconds=timeout + 120.0,
        )
        elapsed = time.monotonic() - started
        after = self.session.relay_request_count()
        events = []
        for line in stdout.splitlines():
            line = line.strip()
            if line.startswith("{"):
                try:
                    events.append(json.loads(line))
                except Exception:
                    pass
        record = {
            "resume": resume,
            "prompt": prompt,
            "exit_code": code,
            "elapsed_seconds": round(elapsed, 2),
            "relay_requests_before": before,
            "relay_requests_after": after,
            "relay_requests_delta": after - before,
            "events": events,
            "stderr_tail": stderr[-4000:],
        }
        self.calls.append(record)
        return record


def banner(record: dict) -> dict:
    for item in record["events"]:
        if "permission_mode" in item or item.get("type") == "system":
            return item
    return {}


def usage(record: dict) -> dict:
    """Provider-reported usage only. Last usage-bearing event wins."""
    found: dict = {}
    for item in record["events"]:
        block = item.get("usage")
        if isinstance(block, dict) and block:
            found.update(block)
        for key in ("input_tokens", "output_tokens", "cached_input_tokens"):
            if isinstance(item.get(key), int):
                found[key] = item[key]
    return found


def compaction_hits(record: dict) -> list:
    """Events that name the CLI's own compaction. Not inferred from counts."""
    markers = ("compact_summary", "chatCompression", "compress", "compaction")
    return [
        item for item in record["events"] if any(m in json.dumps(item) for m in markers)
    ]


def profile_of(record: dict) -> dict:
    tools = set(banner(record).get("tools", []))
    return {
        "permission_mode": banner(record).get("permission_mode"),
        "tool_count": len(tools),
        "native_edit": sorted(tools & NATIVE_EDIT),
        "native_shell": sorted(tools & NATIVE_SHELL),
        "computer_use_surface": sorted(t for t in tools if t.startswith("computer_use")),
        "tool_search_surface": sorted(t for t in tools if "tool_search" in t),
    }


def build_config(seed_commit: str, task_sha: str) -> dict:
    payload = json.loads(Path("/probe/workcell-config.json").read_text())
    payload["pin"]["seed_commit"] = seed_commit
    payload["pin"]["task_artifact_sha256"] = task_sha
    payload["workspace_host_path"] = str(HOST_ROOT / "clone")
    payload["task_artifact_host_path"] = str(HOST_ROOT / "task/task.md")
    egress = payload["egress"]
    egress["relay"]["socket_path"] = str(HOST_ROOT / "socket-live/model.sock")
    egress["forwarder_host_path"] = str(HOST_ROOT / "controller/forwarder.py")
    egress["envelope_socket_host_directory"] = str(HOST_ROOT / "socket-envelope")
    return payload


def stage_1_containment(session: LiveWorkcellSession) -> dict:
    """Containment before anything is spent. Zero model tokens."""
    observations = []
    for probe in DEFAULT_CONTAINMENT_PROBES:
        # `argv`, not a shell string: probes are executed directly so a
        # shell cannot reinterpret them into something else.
        code, stdout, stderr = session.exec(probe.argv, timeout_seconds=45.0)
        observations.append(
            classify_probe(probe, exit_code=code, stdout=stdout, stderr=stderr)
        )
    report = evaluate_containment(observations)
    write("stage1-containment.json", report.model_dump(mode="json"))
    return {"passed": report.passed, "detail": report.detail}


def stage_1b_no_direct_upstream(session: LiveWorkcellSession) -> dict:
    """The workcell must not be able to reach the model on its own.

    Explicit, because containment probes prove there is no route *in general*
    while this proves there is no route to *this* upstream -- which is the one
    that would silently invalidate every measurement below.
    """
    host = UPSTREAM.split("//", 1)[-1]
    code, stdout, stderr = session.exec(
        [
            "sh",
            "-c",
            f"python3 -c \"import socket,sys;"
            f"h='{host.split(':')[0]}';p=int('{(host.split(':') + ['80'])[1]}');"
            f"s=socket.socket();s.settimeout(4);"
            f"sys.exit(0 if s.connect_ex((h,p))==0 else 7)\"",
        ],
        timeout_seconds=60.0,
    )
    reachable = code == 0
    result = {
        "upstream": UPSTREAM,
        "directly_reachable_from_workcell": reachable,
        "exit_code": code,
        "stderr_tail": stderr[-500:],
        "passed": not reachable,
    }
    write("stage1b-no-direct-upstream.json", result)
    return result


def main() -> int:
    EV.mkdir(parents=True, exist_ok=True)
    for name in ("task", "controller"):
        (HOST_ROOT / name).mkdir(parents=True, exist_ok=True)
    Path(HOST_ROOT / "controller/forwarder.py").write_bytes(
        Path("/opt/apoapsis/src/apoapsis/workcell/forwarder.py").read_bytes()
    )
    task_path = HOST_ROOT / "task/task.md"
    task_path.write_text(TASK_TEXT, encoding="utf-8")

    subprocess.run(
        ["git", "config", "--global", "--add", "safe.directory", str(SOURCE_REPO)],
        check=False,
    )
    clone = create_sanitized_clone(
        source_repository=SOURCE_REPO,
        clone_path=HOST_ROOT / "clone",
        task_artifact_source=task_path,
        task_artifact_destination=task_path,
        workspace_owner="65532:65532",
    )
    write("stage0-clone.json", clone.model_dump(mode="json"))
    if not clone.sanitized:
        write("summary.json", {"verdict": "CLONE_NOT_SANITIZED"})
        return 2

    config = WorkcellConfig.model_validate(
        build_config(clone.seed_commit, clone.task_artifact_sha256)
    )
    write("workcell-config.json", config.model_dump(mode="json"))

    summary: dict = {"upstream": UPSTREAM, "stages": {}}

    with LiveWorkcellSession(config) as session:
        summary["stages"]["1_containment"] = stage_1_containment(session)
        if not summary["stages"]["1_containment"]["passed"]:
            write("summary.json", summary | {"verdict": "CONTAINMENT_FAILED"})
            return 3

        summary["stages"]["1b_no_direct_upstream"] = stage_1b_no_direct_upstream(
            session
        )
        if not summary["stages"]["1b_no_direct_upstream"]["passed"]:
            write("summary.json", summary | {"verdict": "DIRECT_UPSTREAM_REACHABLE"})
            return 4

        code, stderr = session.start_forwarder()
        summary["stages"]["1c_forwarder"] = {"exit_code": code, "stderr": stderr[-500:]}

        from apoapsis.workcell.controller import check_relay_readiness

        readiness = check_relay_readiness(session.controller)
        write("stage1d-relay-readiness.json", readiness.model_dump(mode="json"))
        summary["stages"]["1d_relay_readiness"] = {
            "ready": readiness.ready,
            "detail": readiness.detail,
        }
        if not readiness.ready:
            write("summary.json", summary | {"verdict": "RELAY_NOT_READY"})
            return 7

        # CLI settings: the pinned yolo coding profile, pointed at the loopback
        # forwarder rather than at any upstream the workcell could name.
        settings = json.loads(Path("/probe/qwen-settings-yolo.json").read_text())
        base = f"http://127.0.0.1:{config.egress.loopback_port}/v1"
        settings["modelProviders"]["openai"][0]["baseUrl"] = base
        session.exec(
            [
                "sh",
                "-c",
                f"mkdir -p {CLI_HOME}/.qwen && cat > {CLI_HOME}/.qwen/settings.json",
            ],
            timeout_seconds=60.0,
        )
        # `exec` has no stdin channel here, so the settings are written with a
        # quoted heredoc: no interpolation, and nothing to quote wrong.
        session.exec(
            [
                "sh",
                "-c",
                f"mkdir -p {CLI_HOME}/.qwen && cat > {CLI_HOME}/.qwen/settings.json "
                f"<<'APOAPSIS_EOF'\n{json.dumps(settings, indent=2)}\nAPOAPSIS_EOF",
            ],
            timeout_seconds=60.0,
        )
        session.exec(
            [
                "sh",
                "-c",
                f"cat > {CLI_HOME}/.qwen/.env <<'APOAPSIS_EOF'\n"
                f"OPENAI_API_KEY=local-no-auth\nOPENAI_BASE_URL={base}\n"
                f"OPENAI_MODEL=qwen3.6-27b\nAPOAPSIS_EOF",
            ],
            timeout_seconds=60.0,
        )
        # A tiny, dependent two-step working set: stage 6's edit must depend on
        # stage 2's, or "it continued" would not distinguish continuation from
        # a fresh start that happened to succeed.
        session.exec(
            [
                "sh",
                "-c",
                f"cd {WORKDIR} && cat > calc.py <<'APOAPSIS_EOF'\n"
                "def add(a, b):\n    return a + b\n"
                "APOAPSIS_EOF\n"
                "cat > run_tests.py <<'APOAPSIS_EOF'\n"
                "from calc import add\n\n\ndef main():\n"
                "    assert add(2, 3) == 5\n    print('OK')\n\n\n"
                "if __name__ == '__main__':\n    main()\n"
                "APOAPSIS_EOF",
            ],
            timeout_seconds=60.0,
        )

        qwen = Qwen(session)

        # -- stage 2: fresh -p turn ------------------------------------
        first = qwen(
            "Add a subtract(a, b) function to calc.py returning a - b. "
            "Then run 'python3 run_tests.py' and report exactly what it printed."
        )
        write("stage2-fresh.json", first)
        head = banner(first)
        session_id = head.get("session_id") or head.get("sessionId")
        summary["stages"]["2_fresh"] = {
            "exit_code": first["exit_code"],
            "session_id": session_id,
            "profile": profile_of(first),
            "usage": usage(first),
            "relay_requests_delta": first["relay_requests_delta"],
        }
        # The containment assertion that matters most: a turn that worked and
        # produced no relay traffic reached the model some other way.
        if first["exit_code"] == 0 and first["relay_requests_delta"] == 0:
            write("summary.json", summary | {"verdict": "ZERO_RELAY_TRAFFIC"})
            return 5

        code, listing, _ = session.exec(
            ["sh", "-c", f"HOME={CLI_HOME} qwen sessions list 2>&1 | head -40"],
            timeout_seconds=120.0,
        )
        write("stage2-sessions-list.txt", listing)
        summary["stages"]["2_fresh"]["sessions_list_tail"] = listing[-2000:]

        if not session_id:
            write("summary.json", summary | {"verdict": "NO_SESSION_ID"})
            return 6

        # -- stage 3+4: resume, same container/HOME/cwd, verify profile --
        second = qwen(
            "What is the name of the function you added to calc.py in this "
            "session? Answer with only the function name.",
            resume=session_id,
        )
        write("stage3-resume.json", second)
        summary["stages"]["3_resume"] = {
            "exit_code": second["exit_code"],
            "relay_requests_delta": second["relay_requests_delta"],
            "usage": usage(second),
        }
        summary["stages"]["4_resumed_profile"] = profile_of(second)

        # -- stage 5: cross the native threshold ----------------------
        # Grow the conversation with real work until provider-reported input
        # crosses the resolved native threshold. The threshold is Qwen's, not
        # ours: we do not compact anything here, we only watch for the CLI's
        # own compaction event.
        native = config.pin.native_context
        limit = config.pin.model.context_limit_tokens
        trigger = native.auto_compact_threshold * limit
        pressure_log = []
        compaction_seen: list = []
        turn = 0
        while turn < 8 and not compaction_seen:
            turn += 1
            probe = qwen(
                "Read every file in this directory, then write a detailed "
                f"note into notes_{turn}.md explaining what run_tests.py "
                "verifies and how calc.py implements it. Be thorough and "
                "quote the code you read.",
                resume=session_id,
            )
            write(f"stage5-pressure-turn{turn}.json", probe)
            reported = usage(probe)
            hits = compaction_hits(probe)
            pressure_log.append(
                {
                    "turn": turn,
                    "usage": reported,
                    "input_tokens": reported.get("input_tokens"),
                    "utilisation": (
                        round(reported.get("input_tokens", 0) / limit, 4)
                        if reported.get("input_tokens")
                        else None
                    ),
                    "compaction_events": len(hits),
                    "exit_code": probe["exit_code"],
                }
            )
            if hits:
                compaction_seen = hits
            write("stage5-pressure.json", {"trigger_tokens": trigger,
                                           "native_threshold": native.auto_compact_threshold,
                                           "log": pressure_log})
        summary["stages"]["5_native_compaction"] = {
            "native_threshold": native.auto_compact_threshold,
            "threshold_tokens": trigger,
            "turns_run": turn,
            "log": pressure_log,
            "compaction_observed": bool(compaction_seen),
            # An observed event or nothing. Never inferred from a token count
            # dropping, which could equally be the model writing less.
            "verdict": (
                "NATIVE_COMPACTION_OBSERVED"
                if compaction_seen
                else "NO_COMPACTION_EVENT_OBSERVED -- context safety unproven"
            ),
        }
        write("stage5-compaction-events.json", compaction_seen)

        # -- stage 6: capsule injection + dependent verified edit ------
        capsule = Path("/probe/capsule.md")
        capsule_text = (
            capsule.read_text(encoding="utf-8")
            if capsule.exists()
            else "# State\n\nStill outstanding: calc.py needs a multiply(a, b).\n"
        )
        sixth = qwen(
            capsule_text
            + "\n\nUsing the subtract function you added earlier in this "
            "session, add multiply(a, b) to calc.py implemented as repeated "
            "addition, extend run_tests.py to assert multiply(4, 3) == 12 "
            "AND assert subtract(5, 3) == 2, then run "
            "'python3 run_tests.py' and report exactly what it printed.",
            resume=session_id,
        )
        write("stage6-capsule-resume.json", sixth)
        code_after, calc_after, _ = session.exec(
            ["sh", "-c", f"cd {WORKDIR} && cat calc.py && echo '---' && "
             "python3 run_tests.py 2>&1 | tail -5"],
            timeout_seconds=180.0,
        )
        write("stage6-verification.txt", calc_after)
        summary["stages"]["6_capsule_dependent_edit"] = {
            "exit_code": sixth["exit_code"],
            "verification_exit_code": code_after,
            "verification_tail": calc_after[-1500:],
            # Controller-verified, not model-reported.
            "dependent_edit_verified": code_after == 0
            and "multiply" in calc_after
            and "subtract" in calc_after
            and "OK" in calc_after,
        }

        # -- stage 7: stable vs perturbed kernel cache control ---------
        kernel = "# Task\n\nAnswer the question at the end. Do not use tools.\n" + (
            "This is fixed context that must be byte-identical between the two "
            "arms of this control so that any cached-input difference is "
            "attributable to the perturbation alone. " * 40
        )
        arms: dict = {}
        for arm in ("stable", "perturbed"):
            reads = []
            for index in range(3):
                prefix = (
                    kernel
                    if arm == "stable"
                    else kernel.replace("# Task", f"# Task {index}", 1)
                )
                run = qwen(
                    prefix + f"\n\nQuestion: reply with the single word ARM{index}.",
                    timeout=300.0,
                )
                write(f"stage7-{arm}-{index}.json", run)
                reads.append(
                    {
                        "index": index,
                        "usage": usage(run),
                        "elapsed_seconds": run["elapsed_seconds"],
                        "exit_code": run["exit_code"],
                    }
                )
            arms[arm] = reads
        cached_reported = any(
            isinstance(item["usage"].get("cached_input_tokens"), int)
            for reads in arms.values()
            for item in reads
        )
        summary["stages"]["7_cache_control"] = {
            "arms": arms,
            "cached_input_telemetry_present": cached_reported,
            # Latency alone is insufficient and is not used as a fallback.
            "verdict": (
                "MEASURED" if cached_reported else "NOT_MEASURABLE -- the server "
                "reported no cached-input tokens; latency alone is insufficient"
            ),
        }
        write("stage7-cache-control.json", summary["stages"]["7_cache_control"])

        summary["verdict"] = {
            "context_safety": summary["stages"]["5_native_compaction"]["verdict"],
            "post_compaction_continuation": (
                "VERIFIED"
                if summary["stages"]["6_capsule_dependent_edit"][
                    "dependent_edit_verified"
                ]
                else "NOT_VERIFIED"
            ),
            "efficiency": summary["stages"]["7_cache_control"]["verdict"],
        }
        write("summary.json", summary)
        print(json.dumps(summary, indent=2)[:6000])
    return 0


if __name__ == "__main__":
    sys.exit(main())
