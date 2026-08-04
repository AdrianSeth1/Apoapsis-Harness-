"""One model server for a plan run, verified before each arm.

`ModelServer` cold-starts `llama-server` per arm per attempt, which means the
16.8 GB Qwen3.6-27B GGUF is loaded from disk again every time. With the parity
guard on that is twice per slice, and aborted attempts multiply it: one task in
`test project 6` accumulated 17 CAP directories for roughly five slice
executions. This, far more than inference, is why a plan run feels slow.

The reason it was written that way is sound: provenance. Every evidence record
is bound to a server whose identity was established for *this* run, and a
resident process that nobody checked is exactly the thing a frozen manifest
exists to rule out.

But reloading is not what establishes provenance — *checking* is. A resident
server can be verified against the manifest before each arm: the weights it has
open, the alias it serves, and the argv it is running under are all observable
from outside the process, and observing them is strictly more evidence than
re-running a load that was only ever trusted because it was recent.

Two rules make that safe, and both are enforced here rather than left to the
caller:

**Verification is per arm, never per lease.** A server verified at the start of
a plan run and used for twelve slices afterwards is a server whose identity is
twelve slices old. Each arm re-observes.

**A failed check never degrades into a warning.** `verified` goes false, the
mismatch is recorded with what was expected and what was found, and the caller
cold-starts instead. There is no path here that serves an unverified server,
because a run that quietly continued against the wrong weights would produce
evidence that looks exactly like evidence.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Callable, Protocol

from pydantic import Field

from apoapsis.specification.schema import StrictModel, utc_now

#: `(path, method, body) -> (status, payload)`. Injected so the lease's logic
#: is testable without a 16.8 GB model: the checks are the interesting part and
#: they should not require the thing they exist to avoid restarting.
Transport = Callable[[str, str, dict | None], tuple[int, dict]]


class ProvenanceMismatch(StrictModel):
    """One thing the running server disagrees with the manifest about."""

    field: str = Field(min_length=1)
    expected: str
    observed: str


class LeaseVerification(StrictModel):
    """What was observed about the resident server, before one arm ran."""

    at: datetime = Field(default_factory=utc_now)
    arm: str = ""
    verified: bool = False
    #: Every check that ran and passed, by name, so "verified" is never a bare
    #: assertion: a verification that checked nothing and a verification that
    #: checked four things both report `verified`, and only this tells them
    #: apart.
    checks_passed: list[str] = Field(default_factory=list)
    #: Checks that could not run at all -- an endpoint this build does not
    #: serve, a `/proc` entry that is not readable. Recorded separately from
    #: mismatches, because "we could not look" and "we looked and it differs"
    #: are different findings with different fixes.
    checks_unavailable: list[str] = Field(default_factory=list)
    mismatches: list[ProvenanceMismatch] = Field(default_factory=list)
    detail: str = ""


class SlotReset(StrictModel):
    """The KV cache erase performed between arms.

    Recorded rather than assumed. Two arms sharing a process share its cache,
    and a prefix left behind by the control arm would make the sandbox arm's
    first prefill cheaper for a reason that has nothing to do with the sandbox.
    """

    at: datetime = Field(default_factory=utc_now)
    arm: str = ""
    performed: bool = False
    detail: str = ""


class LeaseRecord(StrictModel):
    """The lease's whole life, for the run's evidence directory."""

    schema_version: str = "1.0"
    #: How many times the model was actually loaded. The number this change
    #: exists to reduce; reported so the reduction is a measurement.
    server_starts: int = Field(default=0, ge=0)
    arms_served: int = Field(default=0, ge=0)
    verifications: list[LeaseVerification] = Field(default_factory=list)
    resets: list[SlotReset] = Field(default_factory=list)
    fallbacks: list[str] = Field(default_factory=list)


class ServerProcess(Protocol):
    """The part of `ModelServer` a lease needs."""

    def __enter__(self): ...

    def __exit__(self, *exc: object) -> None: ...

    def readiness(self) -> dict[str, object]: ...


def _running_argv(pid: int | None) -> list[str] | None:
    """The argv the process is *actually* running under, from `/proc`.

    Not the argv we intended to pass: reading it back is what makes this a
    check rather than a memory. `None` when it cannot be read, which is a
    recorded unavailability and never a pass.
    """

    if pid is None:
        return None
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return None
    parts = [item.decode("utf-8", "replace") for item in raw.split(b"\0") if item]
    return parts or None


class ModelServerLease:
    """A resident server for one plan run, re-verified before each arm."""

    def __init__(
        self,
        manifest,
        evidence: Path,
        *,
        server_factory: Callable[[Path], ServerProcess],
        transport: Transport,
    ) -> None:
        self.manifest = manifest
        self.evidence = evidence
        self._server_factory = server_factory
        self._transport = transport
        self._server: ServerProcess | None = None
        self.record = LeaseRecord()

    # -- lifecycle ---------------------------------------------------------

    def acquire(self) -> None:
        """Start the one server this run will use, if it is not already up."""

        if self._server is not None:
            return
        server = self._server_factory(self.evidence / "server")
        server.__enter__()
        self._server = server
        self.record.server_starts += 1

    def release(self) -> None:
        if self._server is None:
            return
        server, self._server = self._server, None
        server.__exit__(None, None, None)

    def __enter__(self) -> ModelServerLease:
        self.acquire()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.release()
        self.write_evidence()

    # -- per-arm ----------------------------------------------------------

    def verify(self, arm: str) -> LeaseVerification:
        """Re-observe the resident server against the frozen manifest."""

        verification = LeaseVerification(arm=arm)
        passed: list[str] = []
        unavailable: list[str] = []
        mismatches: list[ProvenanceMismatch] = []

        status, _payload = self._call("/health")
        if status == 200:
            passed.append("health")
        else:
            mismatches.append(
                ProvenanceMismatch(
                    field="health", expected="200", observed=str(status)
                )
            )

        status, props = self._call("/props")
        if status != 200:
            unavailable.append("props")
        else:
            observed_path = str(
                props.get("model_path")
                or (props.get("default_generation_settings") or {}).get("model")
                or ""
            )
            expected_path = str(self.manifest.model.absolute_path)
            if not observed_path:
                unavailable.append("model_path")
            elif observed_path != expected_path:
                mismatches.append(
                    ProvenanceMismatch(
                        field="model_path",
                        expected=expected_path,
                        observed=observed_path,
                    )
                )
            else:
                passed.append("model_path")

        status, models = self._call("/v1/models")
        if status != 200:
            unavailable.append("model_alias")
        else:
            served = [
                str(item.get("id"))
                for item in (models.get("data") or [])
                if item.get("id")
            ]
            expected_alias = str(self.manifest.model.model_alias)
            if not served:
                unavailable.append("model_alias")
            elif expected_alias not in served:
                mismatches.append(
                    ProvenanceMismatch(
                        field="model_alias",
                        expected=expected_alias,
                        observed=", ".join(served),
                    )
                )
            else:
                passed.append("model_alias")

        argv = _running_argv(self._server_pid())
        expected_argv = list(self.manifest.server.argv)
        if argv is None:
            unavailable.append("argv")
        elif argv != expected_argv:
            mismatches.append(
                ProvenanceMismatch(
                    field="argv",
                    expected=json.dumps(expected_argv),
                    observed=json.dumps(argv),
                )
            )
        else:
            passed.append("argv")

        verification.checks_passed = passed
        verification.checks_unavailable = unavailable
        verification.mismatches = mismatches
        # Health and the weights are the two that decide it. An unavailable
        # check is not a pass, so a build that serves neither `/props` nor
        # `/v1/models` cannot be leased -- it cold-starts instead, which is the
        # old behaviour and therefore never worse than before.
        verification.verified = (
            not mismatches
            and "health" in passed
            and ("model_path" in passed or "model_alias" in passed)
        )
        verification.detail = (
            "the resident server matches the frozen manifest"
            if verification.verified
            else "; ".join(
                f"{item.field}: expected {item.expected}, observed {item.observed}"
                for item in mismatches
            )
            or "no check could establish the resident server's identity"
        )
        self.record.verifications.append(verification)
        if verification.verified:
            self.record.arms_served += 1
        else:
            self.record.fallbacks.append(
                f"{arm}: {verification.detail}"
            )
        return verification

    def reset_slots(self, arm: str) -> SlotReset:
        """Erase the KV cache so no state crosses between arms."""

        status, _payload = self._call("/slots?action=erase", method="POST", body={})
        performed = status in {200, 202, 204}
        reset = SlotReset(
            arm=arm,
            performed=performed,
            detail=(
                "slot cache erased between arms"
                if performed
                else f"the server did not erase its slots (status {status}); "
                "the next arm may reuse a prefix this one left behind"
            ),
        )
        self.record.resets.append(reset)
        return reset

    def write_evidence(self) -> None:
        self.evidence.mkdir(parents=True, exist_ok=True)
        (self.evidence / "model-server-lease.json").write_text(
            json.dumps(self.record.model_dump(mode="json"), indent=2, sort_keys=True)
            + "\n",
            encoding="utf-8",
        )

    def _server_pid(self) -> int | None:
        """The PID of the process this lease started, or `None`.

        `None` when the lease did not start the server itself, which is why the
        argv check reports as unavailable rather than as a pass: a process
        somebody else started is one this lease cannot vouch for by memory.
        """

        process = getattr(self._server, "process", None)
        return getattr(process, "pid", None)

    # -- transport --------------------------------------------------------

    def _call(
        self, path: str, method: str = "GET", body: dict | None = None
    ) -> tuple[int, dict]:
        try:
            return self._transport(path, method, body)
        except Exception as exc:  # noqa: BLE001 - any failure is "cannot check"
            return 0, {"error": str(exc)}


__all__ = [
    "LeaseRecord",
    "LeaseVerification",
    "ModelServerLease",
    "ProvenanceMismatch",
    "SlotReset",
    "Transport",
]
