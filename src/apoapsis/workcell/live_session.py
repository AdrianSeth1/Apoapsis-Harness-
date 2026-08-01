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

from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

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
from apoapsis.workcell.conformance import (
    ConformanceCheck,
    ConformanceReport,
    evaluate_conformance,
)
from apoapsis.workcell.conformance_driver import (
    DeclaredCliLimits,
    LiveConformanceRunner,
)
from apoapsis.workcell.echo_provider import DeterministicEchoProvider
from apoapsis.workcell.pins import WorkcellConfig
from apoapsis.workcell.platform_support import prepare_socket_directory
from apoapsis.workcell.relay import ModelRelay
from apoapsis.workcell.relay_policy import ModelRelayConfig
from apoapsis.workcell.transcription import TranscriptionFidelity
from apoapsis.workcell.relay_preflight import RelayReadinessReport


class LiveWorkcellSession:
    """Owns the relay and the container for the duration of one session."""

    def __init__(self, config: WorkcellConfig) -> None:
        self.config = config
        self.controller = WorkcellController(config)
        self.relay = ModelRelay(config.egress.relay)
        self._started = False
        #: The echo provider from the most recent `envelope_path` block, kept
        #: after teardown so its captured request bytes remain readable as
        #: evidence.
        self._last_echo_provider: DeterministicEchoProvider | None = None

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
        # Prepared before the container exists for the same reason as the model
        # socket directory: Docker creates a missing bind-mount source as a
        # root-owned directory the workcell cannot use.
        envelope_directory = self.config.egress.envelope_socket_host_directory
        if envelope_directory:
            Path(envelope_directory).mkdir(parents=True, exist_ok=True)
            self._grant_group(Path(envelope_directory))
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

        self._grant_group(Path(self.config.egress.socket_host_directory))

    def _grant_group(self, directory: Path) -> None:
        import os
        import stat

        _, _, gid_text = self.config.user.partition(":")
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

    def incomplete_relay_responses(self) -> tuple[str, ...]:
        """Turns whose bytes are a fragment, as the relay recorded them.

        A controller must consult this before treating anything the agent
        produced as a proposal. A stream that ended without its terminal event
        carries a 200 and a partial body, and by inspection that is
        indistinguishable from a short answer -- so the distinction has to come
        from the relay, which watched the transfer end.
        """

        return tuple(
            f"{item.method} {item.raw_path}: {item.detail or 'response incomplete'}"
            for item in self.relay.stats.incomplete_records
        )

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

    # -- the deterministic envelope path (ADR 0078) ------------------------

    @contextmanager
    def envelope_path(self) -> Iterator[str]:
        """Stand up a second relay whose upstream is the echo provider.

        Everything but the model is real: a `DeterministicEchoProvider` on the
        controller, a second `ModelRelay` binding a second socket in the same
        controller-owned directory, and a second in-container forwarder on a
        second loopback port. The workcell reaches it exactly the way it
        reaches the model -- loopback, socket, relay -- which is the whole point
        of ADR 0078's insistence that the check run through the real path.

        The echo socket lives in its own dedicated directory, mounted at
        container creation. The first version of this put it beside the model
        socket, and `prepare_socket_directory` refused: a socket directory must
        contain exactly one socket, or mounting it becomes an unmediated
        channel. That refusal was correct and is why there are two directories.

        Yields the in-container base URL. Both halves are torn down on exit,
        including on failure -- an echo provider left running would be an
        unpinned second upstream, which is precisely what the relay exists to
        make impossible.
        """

        egress = self.config.egress
        if not egress.envelope_socket_host_directory:
            raise RuntimeError(
                "no envelope socket directory is configured, so the ADR 0078 "
                "echo path cannot be stood up; the envelope check will report "
                "NOT_RUN rather than fall back on a model measurement"
            )
        echo_port = egress.loopback_port + 1
        socket_directory = Path(egress.envelope_socket_host_directory)
        echo_socket = socket_directory / "echo.sock"
        prepare_socket_directory(str(echo_socket))
        self._grant_group(socket_directory)
        provider = DeterministicEchoProvider(model=self.config.pin.model.model_name)
        provider.start()
        relay = ModelRelay(
            ModelRelayConfig(
                upstream_base_url=provider.base_url,
                socket_path=str(echo_socket),
                # Narrowed to the one route the probe uses. The echo path has no
                # business being reachable for anything else, and narrowing is
                # the only direction `allowed_routes` permits.
                allowed_routes=["/v1/chat/completions", "/health"],
                max_total_requests=16,
            )
        )
        relay.start()
        self._last_echo_provider = provider
        try:
            container_socket = (
                f"{egress.envelope_socket_container_directory.rstrip('/')}/echo.sock"
            )
            command = (
                f"nohup python3 {egress.forwarder_container_path} "
                f"--port {echo_port} --socket {container_socket} "
                f"> /tmp/apoapsis-echo-forwarder.log 2>&1 & sleep 2"
            )
            self.exec(["sh", "-c", command], timeout_seconds=40.0)
            yield f"http://127.0.0.1:{echo_port}"
        finally:
            relay.stop()
            provider.stop()

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
        """Drive the nine checks through the relay and evaluate them.

        Eight run against `llama-server`. `multiline_unicode_integrity` runs
        against the deterministic echo path (ADR 0078), which is stood up and
        torn down around it rather than kept open for the whole suite: a second
        upstream that outlives the one check that needs it is a second upstream
        the rest of the suite could accidentally use.
        """

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
        results: list = []
        with self.envelope_path() as envelope_base_url:
            runner.envelope_base_url = envelope_base_url.rstrip("/")
            envelope_result = runner.run_multiline_unicode_integrity()
            provider = self._last_echo_provider
            exchange = provider.last_exchange() if provider is not None else None
            if exchange is not None and exchange.payload is not None:
                # Re-decide with the *captured* request bytes now that the
                # provider has them. Comparing the response against the probe
                # constant would leave the inbound half of the round trip
                # unverified, which the first pass says out loud.
                runner.captured_envelope_bytes = exchange.payload.encode("utf-8")
                envelope_result = runner.run_multiline_unicode_integrity()
        results.append(envelope_result)
        runner.envelope_base_url = None
        results.extend(
            check
            for check in runner.run_all()
            if check.check is not ConformanceCheck.MULTILINE_UNICODE_INTEGRITY
        )
        report = evaluate_conformance(
            results, workcell_manifest_digest=pin.manifest_digest()
        )
        return report, runner

    def run_transcription_fidelity(
        self, runner: LiveConformanceRunner
    ) -> TranscriptionFidelity:
        """Measure the model's transcription accuracy. Never gates.

        Kept out of `run_conformance` so that no future edit can accidentally
        fold its result into `evaluate_conformance`: the two live in different
        methods and different modules, and this one returns a type that has no
        `ConformanceStatus` to fold.
        """

        return runner.run_transcription_fidelity()


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
