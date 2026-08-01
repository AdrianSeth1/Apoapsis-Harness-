"""Observations, not assertions.

The previous runner recorded `arm_visible_mounts_verified=True` and passed
literal `0` for surviving workers and streams. Those are not placeholders
somebody forgot to fill in -- they are values that *look* like measurements and
were written by hand, which is worse, because a reader has no way to tell them
apart from the real ones.

Everything here answers its question by looking. Each function returns what it
saw, including when what it saw is bad news, and none of them has a default
that means "fine".
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from pydantic import Field

from apoapsis.specification.schema import StrictModel


#: Substrings that mean the tool rejected the *arguments* and never opened a
#: socket. These must never count as containment: the development probe sent
#: `url` without `prompt`, got "params must have required property 'prompt'",
#: and a classifier that treated any error as a refusal would have recorded
#: that schema complaint as proof the network boundary held.
SCHEMA_REJECTION_MARKERS = ("must have required property", "invalid params")

#: Substrings that mean name resolution or connection genuinely failed. This
#: is the category, not the wording: `EAI_AGAIN` is one DNS failure spelling
#: among several and the bound evidence is "DNS/network unreachable", so a
#: different resolver phrasing satisfies the same requirement.
NETWORK_UNREACHABLE_MARKERS = (
    "enotfound",
    "eai_again",
    "econnrefused",
    "getaddrinfo",
    "enetunreach",
    "ehostunreach",
    "network is unreachable",
    "timed out",
    "socket",
)

#: Substrings that would mean the fetch *worked*. Checked independently, so a
#: response carrying both a warning and real page content is a breach.
FETCH_SUCCESS_MARKERS = ("<!doctype html", "example domain", "iana", "http status")

#: A cached answer never touches the network, so it can neither prove nor
#: disprove containment.
CACHE_HIT_MARKERS = ("served from cache", "cached response")


class EgressRefusal(StrictModel):
    """Why an undeclared-egress attempt failed, by category rather than wording.

    Bound as eight independent facts because the interesting failures are the
    ones that look like success. A schema rejection, a cache hit and a real DNS
    failure all produce "the fetch did not return page content", and only the
    last is containment evidence.
    """

    #: The tool accepted the arguments and actually ran.
    invocation_accepted: bool = False
    #: The tool reported reaching for the network, rather than refusing early.
    network_attempted: bool = False
    #: Read from the provider transcript's `tool` message -- the authoritative
    #: channel. The tool result never appears on stdout, so a classifier
    #: reading stdout cannot see this outcome at all.
    tool_transcript_captured: bool = False
    #: No page content came back.
    no_successful_response: bool = False
    #: The failure is DNS or connection level.
    classified_network_unreachable: bool = False
    #: The tool did not merely complain about arguments.
    not_schema_rejection: bool = False
    #: The answer did not come from web_fetch's 15-minute same-URL cache.
    not_cache_hit: bool = False
    #: The verdict was not inferred from stdout.
    not_stdout_inference: bool = False
    tool_results: tuple[str, ...] = ()

    @property
    def refused(self) -> bool:
        """Every category must hold. No single marker is sufficient."""

        return all(
            (
                self.invocation_accepted,
                self.network_attempted,
                self.tool_transcript_captured,
                self.no_successful_response,
                self.classified_network_unreachable,
                self.not_schema_rejection,
                self.not_cache_hit,
                self.not_stdout_inference,
            )
        )

    @property
    def unsatisfied_categories(self) -> tuple[str, ...]:
        checks = {
            "invocation_accepted": self.invocation_accepted,
            "network_attempted": self.network_attempted,
            "tool_transcript_captured": self.tool_transcript_captured,
            "no_successful_response": self.no_successful_response,
            "classified_network_unreachable": self.classified_network_unreachable,
            "not_schema_rejection": self.not_schema_rejection,
            "not_cache_hit": self.not_cache_hit,
            "not_stdout_inference": self.not_stdout_inference,
        }
        return tuple(name for name, ok in checks.items() if not ok)


def observe_egress_refusal(transcript_path: Path) -> EgressRefusal:
    """Classify an egress attempt from the provider transcript.

    The transcript is the only authoritative source: Qwen returns a tool result
    as a `tool` role message to the provider, and it never reaches stdout.
    """

    if not transcript_path.is_file():
        return EgressRefusal()

    results: list[str] = []
    for entry in json.loads(transcript_path.read_text(encoding="utf-8")):
        body = entry.get("body")
        if not isinstance(body, dict):
            continue
        for message in body.get("messages", []):
            if message.get("role") in {"tool", "function"}:
                content = message.get("content")
                results.append(
                    content if isinstance(content, str) else json.dumps(content)
                )

    if not results:
        return EgressRefusal()

    combined = "\n".join(results).lower()
    schema_rejected = any(item in combined for item in SCHEMA_REJECTION_MARKERS)
    cached = any(item in combined for item in CACHE_HIT_MARKERS)
    unreachable = any(item in combined for item in NETWORK_UNREACHABLE_MARKERS)
    succeeded = any(item in combined for item in FETCH_SUCCESS_MARKERS)

    return EgressRefusal(
        invocation_accepted=not schema_rejected,
        network_attempted=not schema_rejected and not cached,
        tool_transcript_captured=True,
        no_successful_response=not succeeded,
        classified_network_unreachable=unreachable and not schema_rejected,
        not_schema_rejection=not schema_rejected,
        not_cache_hit=not cached,
        # True by construction: this function reads the transcript and has no
        # access to stdout, so it cannot infer from it even by mistake.
        not_stdout_inference=True,
        tool_results=tuple(results),
    )


class MountObservation(StrictModel):
    """What the container actually has mounted, read from inside it."""

    entries: tuple[str, ...] = ()
    workspace_present: bool = False
    task_artifact_present: bool = False
    forwarder_present: bool = False
    socket_directory_present: bool = False
    evaluator_only_paths_present: tuple[str, ...] = ()
    read_failed: str | None = None

    @property
    def arm_visible_set_is_correct(self) -> bool:
        return (
            self.read_failed is None
            and self.workspace_present
            and self.task_artifact_present
            and not self.evaluator_only_paths_present
        )


def observe_mounts(
    session,
    *,
    workspace_container_path: str = "/workspace",
    task_container_path: str = "/task/task.md",
    forwarder_container_path: str = "/opt/apoapsis/forwarder.py",
    socket_container_directory: str = "/run/apoapsis",
) -> MountObservation:
    """Read `/proc/self/mounts` inside the workcell.

    Asking the container is the only way to know what the container has. The
    controller's intent is what it asked Docker for; this is what arrived.
    """

    code, out, err = session.exec(
        [
            "sh",
            "-c",
            "cat /proc/self/mounts; echo '---MARK---'; "
            f"test -d {workspace_container_path} && echo WS_OK; "
            f"test -f {task_container_path} && echo TASK_OK; "
            f"test -f {forwarder_container_path} && echo FWD_OK; "
            f"test -d {socket_container_directory} && echo SOCK_OK; "
            # `|| true` matters: this `ls` finds nothing in the healthy case
            # and exits 1, which made the whole probe report exit-failure and
            # return every flag as False -- an absent mount and an unreadable
            # probe reported identically, which is the confusion this module
            # exists to prevent.
            "ls -d /evaluator-only /workspace/evaluator-only "
            "/task/evaluator-only 2>/dev/null || true",
        ]
    )
    if code != 0:
        return MountObservation(read_failed=f"exit {code}: {err[:200]}")

    body, _, tail = out.partition("---MARK---")
    entries = tuple(line.strip() for line in body.splitlines() if line.strip())
    flags = {line.strip() for line in tail.splitlines() if line.strip()}
    leaked = tuple(
        line.strip()
        for line in tail.splitlines()
        if line.strip().endswith("evaluator-only")
    )
    return MountObservation(
        entries=entries,
        workspace_present="WS_OK" in flags,
        task_artifact_present="TASK_OK" in flags,
        forwarder_present="FWD_OK" in flags,
        socket_directory_present="SOCK_OK" in flags,
        evaluator_only_paths_present=leaked,
    )


class RuntimeResidue(StrictModel):
    """What is still running after a slot claims to be finished."""

    surviving_containers: tuple[str, ...] = ()
    surviving_relay_streams: int = Field(default=0, ge=0)
    relay_socket_present: bool = False
    observation_failed: str | None = None

    @property
    def clean(self) -> bool:
        return (
            self.observation_failed is None
            and not self.surviving_containers
            and self.surviving_relay_streams == 0
            and not self.relay_socket_present
        )


def observe_residue(
    *,
    container_name_fragment: str,
    socket_path: Path,
    relay=None,
    runtime: str = "docker",
) -> RuntimeResidue:
    """Ask the runtime what survived, rather than assuming nothing did."""

    try:
        result = subprocess.run(  # noqa: S603 - fixed argv, no shell
            [runtime, "ps", "--format", "{{.Names}}"],
            capture_output=True,
            text=True,
            check=False,
            timeout=120,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return RuntimeResidue(observation_failed=f"{type(exc).__name__}: {exc}")
    if result.returncode != 0:
        return RuntimeResidue(
            observation_failed=f"{runtime} ps exited {result.returncode}"
        )

    surviving = tuple(
        name.strip()
        for name in result.stdout.splitlines()
        if container_name_fragment and container_name_fragment in name
    )
    # Concurrency is read off the relay's own counters when one is available;
    # when the relay is already stopped, an absent socket is the observation.
    streams = 0
    if relay is not None:
        streams = max(0, int(getattr(relay.stats, "peak_concurrent_requests", 0)))
        streams = 0 if not getattr(relay, "_started", False) else streams
    return RuntimeResidue(
        surviving_containers=surviving,
        surviving_relay_streams=streams,
        relay_socket_present=Path(socket_path).exists(),
    )


class WorkcellConfiguration(StrictModel):
    """What authentication-shaped configuration the workcell actually holds.

    `no-token-environment` asks whether a token-like variable is in the
    environment, and it stays exactly as it was. This asks the complementary
    question the environment probe cannot: given that the pinned CLI refuses to
    start without a non-empty key, is the value present *the declared
    placeholder*, in the declared place, pointed at the declared endpoint, with
    nothing else credential-shaped anywhere in the box.

    Every field is a separate fact for the same reason `EgressRefusal` is: a
    single boolean would let one satisfied condition stand in for another, and
    "the placeholder is correct" and "no other credentials exist" are different
    claims with different failure modes.
    """

    #: The value found at `security.auth.apiKey`, or `None` if absent. Recorded
    #: verbatim because it is declared non-secret; a secret would never be.
    observed_api_key: str | None = None
    api_key_is_declared_placeholder: bool = False
    #: Where the CLI was told to send it.
    observed_base_url: str | None = None
    base_url_is_bound_loopback: bool = False
    #: Anything else key-shaped: other settings keys, credential files, a token
    #: in the environment after all.
    other_credential_sites: tuple[str, ...] = ()
    #: Container network mode, read from the runtime rather than from intent.
    observed_network_mode: str | None = None
    network_is_none: bool = False
    #: The relay's upstream and route policy, as configured for this run.
    observed_relay_upstream: str | None = None
    observed_relay_routes: tuple[str, ...] = ()
    relay_policy_matches_bound: bool = False
    #: Digest of the exact settings bytes the controller wrote.
    settings_sha256: str | None = None
    settings_digest_matches_bound: bool = False
    detail: str = Field(default="", min_length=0)

    @property
    def satisfied(self) -> bool:
        return (
            self.api_key_is_declared_placeholder
            and self.base_url_is_bound_loopback
            and not self.other_credential_sites
            and self.network_is_none
            and self.relay_policy_matches_bound
            and self.settings_digest_matches_bound
        )

    @property
    def unsatisfied_categories(self) -> tuple[str, ...]:
        failures: list[str] = []
        if not self.api_key_is_declared_placeholder:
            failures.append("api_key_is_declared_placeholder")
        if not self.base_url_is_bound_loopback:
            failures.append("base_url_is_bound_loopback")
        if self.other_credential_sites:
            failures.append("other_credential_sites")
        if not self.network_is_none:
            failures.append("network_is_none")
        if not self.relay_policy_matches_bound:
            failures.append("relay_policy_matches_bound")
        if not self.settings_digest_matches_bound:
            failures.append("settings_digest_matches_bound")
        return tuple(failures)


#: Files that would mean a real credential reached the workcell. Absence is
#: checked from inside, because the controller's intent is not evidence.
CREDENTIAL_PATHS: tuple[str, ...] = (
    "/tmp/qwen-home/.netrc",
    "/tmp/qwen-home/.npmrc",
    "/tmp/qwen-home/.ssh",
    "/tmp/qwen-home/.aws",
    "/tmp/qwen-home/.config/gcloud",
    "/root/.netrc",
    "/root/.ssh",
    "/workspace/.netrc",
    "/workspace/.git-credentials",
)


#: Settings key names that would carry a credential. Matched on the normalised
#: name rather than as a substring: `max_tokens` contains "token" and is a
#: ceiling, and a scanner that cannot tell those apart reports the ceiling as a
#: secret -- which is a false positive that trains readers to ignore the check.
_CREDENTIAL_KEY_NAMES: frozenset[str] = frozenset(
    {
        "apikey",
        "api_key",
        "token",
        "accesstoken",
        "access_token",
        "refreshtoken",
        "refresh_token",
        "authtoken",
        "auth_token",
        "bearertoken",
        "bearer_token",
        "secret",
        "clientsecret",
        "client_secret",
        "password",
        "passwd",
        "credential",
        "credentials",
    }
)


def _is_credential_key(key: str) -> bool:
    """Whether this key name would carry a credential.

    Suffix matching, not substring: `githubToken` and `clientSecret` are
    credentials, while `max_tokens` is a ceiling. The caller additionally
    requires a non-empty *string* value, which is what actually keeps numeric
    ceilings out; this decides the name.
    """

    flattened = key.replace("-", "").replace("_", "").lower()
    if flattened in {name.replace("_", "") for name in _CREDENTIAL_KEY_NAMES}:
        return True
    return any(
        flattened.endswith(suffix)
        for suffix in ("apikey", "token", "secret", "password", "credential", "credentials")
    )


def observe_workcell_configuration(
    session,
    *,
    declared_placeholder: str,
    declared_base_url: str,
    declared_settings_sha256: str | None,
    declared_relay_upstream: str | None,
    declared_relay_routes: tuple[str, ...] = (),
    settings_path: str = "/tmp/qwen-home/settings.json",
) -> WorkcellConfiguration:
    """Read the settings the CLI will actually use, from inside the container.

    Not from the dict the controller built. The controller's copy is what it
    meant to write; this is what arrived, and the two have already differed once
    in this project over a path.
    """

    import hashlib

    observation = WorkcellConfiguration()
    code, raw, stderr = session.exec(["sh", "-c", f"cat {settings_path}"])
    if code != 0 or not raw.strip():
        observation.detail = (
            f"the workcell holds no readable settings at {settings_path}: "
            f"exit {code}: {stderr.strip()[:200]}"
        )
        return observation

    observation.settings_sha256 = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    observation.settings_digest_matches_bound = (
        declared_settings_sha256 is not None
        and observation.settings_sha256 == declared_settings_sha256
    )

    try:
        settings = json.loads(raw)
    except json.JSONDecodeError as exc:
        observation.detail = f"the settings are not valid JSON: {exc}"
        return observation

    auth = (settings.get("security") or {}).get("auth") or {}
    observation.observed_api_key = auth.get("apiKey")
    observation.api_key_is_declared_placeholder = (
        observation.observed_api_key == declared_placeholder
    )

    providers = (settings.get("modelProviders") or {}).get("openai") or []
    if providers:
        observation.observed_base_url = providers[0].get("baseUrl")
    observation.base_url_is_bound_loopback = (
        observation.observed_base_url == declared_base_url
        and str(observation.observed_base_url).startswith("http://127.0.0.1:")
    )

    # Anything else that looks like a credential, wherever it might be.
    others: list[str] = []
    _, env_hits, _ = session.exec(
        [
            "sh",
            "-c",
            "env | grep -Ei '(TOKEN|SECRET|PASSWORD|API_KEY|CREDENTIAL)=' || true",
        ]
    )
    for line in env_hits.splitlines():
        if line.strip():
            others.append(f"environment: {line.split('=', 1)[0]}")

    for path in CREDENTIAL_PATHS:
        code, _out, _err = session.exec(["test", "-e", path])
        if code == 0:
            others.append(f"file: {path}")

    def walk(node, trail: str) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                here = f"{trail}.{key}" if trail else key
                if (
                    here != "security.auth.apiKey"
                    # A credential is a *string*. `max_tokens` matched a naive
                    # substring search for "token" and was reported as a
                    # credential site; its value is 16384. Requiring a non-empty
                    # string separates a secret from a ceiling without needing a
                    # list of exceptions that would grow forever.
                    and isinstance(value, str)
                    and value
                    and _is_credential_key(key)
                ):
                    others.append(f"settings: {here}")
                walk(value, here)
        elif isinstance(node, list):
            for index, value in enumerate(node):
                walk(value, f"{trail}[{index}]")

    walk(settings, "")
    observation.other_credential_sites = tuple(sorted(set(others)))

    config = getattr(session, "config", None)
    egress = getattr(config, "egress", None)
    relay = getattr(egress, "relay", None)
    if relay is not None:
        observation.observed_relay_upstream = relay.upstream_base_url
        observation.observed_relay_routes = tuple(sorted(relay.allowed_routes))
    observation.relay_policy_matches_bound = bool(
        observation.observed_relay_upstream is not None
        and (
            declared_relay_upstream is None
            or observation.observed_relay_upstream == declared_relay_upstream
        )
        and (
            not declared_relay_routes
            or observation.observed_relay_routes == tuple(sorted(declared_relay_routes))
        )
    )

    container = getattr(getattr(config, "pin", None), "container", None)
    name = getattr(container, "image", "")
    _, mode, _ = session.exec(["sh", "-c", "ip route 2>/dev/null | grep -c default || true"])
    # `--network none` leaves loopback and no default route. Read from inside,
    # because the flag the controller passed is intent and this is arrival.
    observation.observed_network_mode = "none" if mode.strip() in {"0", ""} else "routed"
    observation.network_is_none = observation.observed_network_mode == "none"

    observation.detail = (
        f"settings {observation.settings_sha256[:16]} from {name or 'the workcell'}: "
        f"apiKey is the declared placeholder={observation.api_key_is_declared_placeholder}, "
        f"baseUrl={observation.observed_base_url}, "
        f"other credential sites={list(observation.other_credential_sites)}, "
        f"network={observation.observed_network_mode}"
    )
    return observation


class CapabilityProbe(StrictModel):
    """Whether the agent really read, wrote and ran a shell command.

    Behavioural, because the static alternative does not work: a string search
    over the CLI bundle reports `read_file` present because a vendored
    `miniaudio.h` contains those characters, and `edit` because a dependency
    LICENSE does. That check answers yes for the wrong reason, which is the
    exact shape of evidence this project keeps having to remove.
    """

    read_proven: bool = False
    write_proven: bool = False
    shell_proven: bool = False
    controller_visible_bytes: int = Field(default=0, ge=0)
    created_paths: tuple[str, ...] = ()
    detail: str = Field(min_length=1)

    @property
    def satisfied(self) -> bool:
        return self.read_proven and self.write_proven and self.shell_proven


def observe_capability(
    workspace: Path, *, marker: str, shell_artifact: str = "SHELL_RAN"
) -> CapabilityProbe:
    """Judge the probe from the worktree the controller can see.

    The agent's own account is not consulted. Files exist on disk or they do
    not, and their bytes are readable by the controller or they are not.
    """

    created = sorted(
        path.relative_to(workspace).as_posix()
        for path in workspace.rglob("*")
        if path.is_file()
    )
    written = workspace / "probe_written.txt"
    shell_output = workspace / "probe_shell.txt"

    write_proven = written.is_file() and written.read_text(
        encoding="utf-8", errors="replace"
    ).strip() != ""
    shell_proven = shell_output.is_file() and shell_artifact in shell_output.read_text(
        encoding="utf-8", errors="replace"
    )
    # Reading is proven by the agent echoing back a marker only present in the
    # file it was asked to read: it cannot produce the marker without reading.
    read_proven = write_proven and marker in written.read_text(
        encoding="utf-8", errors="replace"
    )

    visible = sum(
        path.stat().st_size for path in workspace.rglob("*") if path.is_file()
    )
    return CapabilityProbe(
        read_proven=read_proven,
        write_proven=write_proven,
        shell_proven=shell_proven,
        controller_visible_bytes=visible,
        created_paths=tuple(created),
        detail=(
            f"read={read_proven} write={write_proven} shell={shell_proven}; "
            f"{len(created)} file(s), {visible} byte(s) visible to the controller"
        ),
    )


class TeardownObservation(StrictModel):
    """Observed absence after a slot, not a cleanup routine's return value."""

    worktree_removed: bool
    qwen_home_removed: bool
    evidence_retained: bool
    evidence_bytes: int = Field(default=0, ge=0)
    residue: RuntimeResidue
    cross_slot_paths_reachable: tuple[str, ...] = ()

    @property
    def clean(self) -> bool:
        return (
            self.worktree_removed
            and self.qwen_home_removed
            and self.evidence_retained
            and self.residue.clean
            and not self.cross_slot_paths_reachable
        )


def observe_teardown(
    *,
    worktree: Path,
    qwen_home: Path,
    evidence: Path,
    residue: RuntimeResidue,
    previous_slot_paths: tuple[Path, ...] = (),
) -> TeardownObservation:
    evidence_bytes = sum(
        path.stat().st_size for path in evidence.rglob("*") if path.is_file()
    )
    return TeardownObservation(
        worktree_removed=not worktree.exists(),
        qwen_home_removed=not qwen_home.exists(),
        evidence_retained=evidence.exists() and evidence_bytes > 0,
        evidence_bytes=evidence_bytes,
        residue=residue,
        cross_slot_paths_reachable=tuple(
            str(path) for path in previous_slot_paths if path.exists()
        ),
    )
