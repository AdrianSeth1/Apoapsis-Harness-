"""One live workcell session: relay up, container up, probes run, both down.

The pieces to do this already existed and none of them were joined together.
`ModelRelay` knew how to listen, `WorkcellController` knew how to start a
container, `containment.py` knew how to classify a probe, and
`conformance_driver.py` knows how to phrase one -- but running the ordered live
sequence still meant an operator gluing them by hand, which is how the first
Slice 2 gate ended up with an unsanitised clone and a root-owned socket.

The ordering here is the handoff's, and it is deliberate:

1. containment probes, which spend no model tokens;
2. relay readiness, which spends exactly one;
3. the nine conformance checks.

A failure at any step stops the ones after it. Running the conformance suite
against a container that failed containment would spend real tokens measuring
an experiment that is already invalid.
"""

from __future__ import annotations

from pathlib import Path

from apoapsis.workcell.containment import (
    DEFAULT_CONTAINMENT_PROBES,
    ContainmentProbe,
    ContainmentReport,
    classify_probe,
    evaluate_containment,
)
from apoapsis.workcell.controller import (
    WorkcellController,
    check_relay_readiness,
)
from apoapsis.workcell.conformance import ConformanceReport, evaluate_conformance
from apoapsis.workcell.conformance_driver import (
    DeclaredCliLimits,
    LiveConformanceRunner,
)
from apoapsis.workcell.pins import WorkcellConfig
from apoapsis.workcell.platform_support import prepare_socket_directory
from apoapsis.workcell.relay import ModelRelay
from apoapsis.workcell.relay_preflight import RelayReadinessReport


class LiveWorkcellSession:
    """Owns the relay and the container for the duration of one session."""

    def __init__(self, config: WorkcellConfig) -> None:
        self.config = config
        self.controller = WorkcellController(config)
        self.relay = ModelRelay(config.egress.relay)
        self._started = False

    def __enter__(self) -> "LiveWorkcellSession":
        # The socket directory is prepared before the relay binds and before the
        # container is created, because the container mounts the directory and a
        # mount of a path that does not yet exist is created by Docker as a
        # root-owned directory the workcell user cannot write.
        prepare_socket_directory(self.config.egress.model_socket_host_path)
        # The directory's group is the only channel by which the controller can
        # hand the workcell access to the socket: the relay assigns the socket
        # the *directory's* gid, and `prepare_socket_directory` preserves setgid
        # but cannot know which group to preserve. Left as the controller's own
        # group (root, in the container that runs this), the socket comes out
        # `root:root` and the first connection from the workcell fails with
        # EACCES -- before a single token is spent, which is the good version of
        # this bug and the reason readiness runs before conformance.
        self._grant_socket_group()
        self.relay.start()
        try:
            self.controller.start()
        except Exception:
            self.relay.stop()
            raise
        self._started = True
        return self

    def __exit__(self, *_exc: object) -> None:
        if self._started:
            self.controller.freeze()
            self.controller.destroy()
        self.relay.stop()
        self._started = False

    def _grant_socket_group(self) -> None:
        """Give the socket directory the workcell's group, and setgid it.

        Only the group is changed. The directory stays owned by the controller
        so the workcell cannot unlink or replace the socket -- it needs to
        connect to the relay, not to be able to stand in for it.
        """

        import os
        import stat

        _, _, gid_text = self.config.user.partition(":")
        directory = Path(self.config.egress.socket_host_directory)
        try:
            os.chown(directory, -1, int(gid_text))
            # setgid so a socket created afterwards inherits the group rather
            # than the creating process's primary group.
            os.chmod(directory, 0o2770 | stat.S_ISGID)
        except (OSError, ValueError) as exc:
            raise RuntimeError(
                f"could not give {directory} the workcell's group {gid_text!r}, "
                "so the relay socket would be unreachable from inside the "
                f"workcell: {exc}"
            ) from exc

    # -- probe transport ---------------------------------------------------

    def exec(
        self, argv: list[str], timeout_seconds: float = 300.0
    ) -> tuple[int | None, str, str]:
        result = self.controller.exec(argv, timeout_seconds=timeout_seconds)
        return result.exit_code, result.stdout, result.stderr

    def relay_request_count(self) -> int:
        return self.relay.stats.total_requests

    def start_forwarder(self, *, settle_seconds: float = 2.0) -> tuple[int | None, str]:
        """Launch the in-container forwarder and leave it running.

        PID 1 in the workcell is `sleep infinity`, so nothing starts the
        forwarder on its own. It is launched detached with its output redirected
        to a file rather than inherited: `docker exec` waits for every writer to
        the captured stream, so a background process holding stdout open would
        hang the call that started it.
        """

        egress = self.config.egress
        command = (
            f"nohup python3 {egress.forwarder_container_path} "
            f"--port {egress.loopback_port} "
            f"--socket {egress.model_socket_container_path} "
            f"> /tmp/apoapsis-forwarder.log 2>&1 & "
            f"sleep {settle_seconds}"
        )
        exit_code, _stdout, stderr = self.exec(
            ["sh", "-c", command], timeout_seconds=settle_seconds + 30.0
        )
        return exit_code, stderr

    # -- the ordered live sequence ----------------------------------------

    def run_containment(
        self, probes: tuple[ContainmentProbe, ...] = DEFAULT_CONTAINMENT_PROBES
    ) -> ContainmentReport:
        """Run every probe from inside the box and fail closed on all of them."""

        results = []
        for probe in probes:
            exit_code, stdout, stderr = self.exec(probe.argv, timeout_seconds=60.0)
            results.append(
                classify_probe(
                    probe, exit_code=exit_code, stdout=stdout, stderr=stderr
                )
            )
        return evaluate_containment(
            results,
            workcell_manifest_digest=self.config.pin.manifest_digest(),
            probes=probes,
        )

    def run_readiness(self) -> RelayReadinessReport:
        before = self.relay_request_count()
        return check_relay_readiness(
            self.controller,
            relay_requests_before=before,
            relay_request_count=self.relay_request_count,
        )

    def run_conformance(
        self,
        *,
        supports_parallel_tool_calls: bool,
        declared_cli_limits: DeclaredCliLimits | None,
        mutating_tool_runner=None,
    ) -> tuple[ConformanceReport, LiveConformanceRunner]:
        """Drive the nine checks through the relay and evaluate them."""

        pin = self.config.pin
        runner = LiveConformanceRunner(
            exec_fn=self.exec,
            base_url=self.config.egress.base_url.rsplit("/v1", 1)[0],
            model_name=pin.model.model_name,
            context_limit_tokens=pin.model.context_limit_tokens,
            server_max_output_tokens=pin.model.max_output_tokens,
            supports_parallel_tool_calls=supports_parallel_tool_calls,
            declared_cli_limits=declared_cli_limits,
            mutating_tool_runner=mutating_tool_runner,
        )
        results = runner.run_all()
        report = evaluate_conformance(
            results, workcell_manifest_digest=pin.manifest_digest()
        )
        return report, runner


def write_evidence(directory: Path, name: str, payload: object) -> Path:
    """Write one evidence document and return where it landed."""

    import json

    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    if hasattr(payload, "model_dump"):
        payload = payload.model_dump(mode="json")  # type: ignore[assignment]
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )
    return path
