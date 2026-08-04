from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from apoapsis.workcell.server_lease import ModelServerLease


class _Manifest:
    """Only the fields the lease reads, so the test needs no pilot manifest."""

    class _Model:
        absolute_path = "/models/qwen3.6-27b-q4.gguf"
        model_alias = "qwen3.6-27b"

    class _Server:
        argv = ("/opt/llama/llama-server", "-m", "/models/qwen3.6-27b-q4.gguf")

    model = _Model()
    server = _Server()


class _FakeServer:
    """Stands in for `ModelServer`: counts loads, holds no weights."""

    starts = 0
    stops = 0

    def __init__(self, evidence: Path) -> None:
        self.evidence = evidence
        self.process = None

    def __enter__(self):
        type(self).starts += 1
        return self

    def __exit__(self, *_exc: object) -> None:
        type(self).stops += 1

    def readiness(self) -> dict[str, object]:
        return {"status": 200}


def _transport(responses: dict[str, tuple[int, dict]]):
    calls: list[tuple[str, str]] = []

    def transport(path: str, method: str = "GET", body: dict | None = None):
        calls.append((method, path))
        return responses.get(path, (404, {}))

    transport.calls = calls  # type: ignore[attr-defined]
    return transport


_HEALTHY = {
    "/health": (200, {}),
    "/props": (200, {"model_path": "/models/qwen3.6-27b-q4.gguf"}),
    "/v1/models": (200, {"data": [{"id": "qwen3.6-27b"}]}),
    "/slots?action=erase": (200, {}),
}


class ModelServerLeaseTests(unittest.TestCase):
    """Reuse is earned by checking, not by assuming."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.evidence = Path(self.temporary.name)
        _FakeServer.starts = 0
        _FakeServer.stops = 0

    def _lease(self, responses: dict) -> ModelServerLease:
        return ModelServerLease(
            _Manifest(),
            self.evidence,
            server_factory=_FakeServer,
            transport=_transport(responses),
        )

    def test_one_load_serves_every_arm_of_the_run(self) -> None:
        with self._lease(_HEALTHY) as lease:
            for arm in ("control", "sandbox", "control-2", "sandbox-2"):
                self.assertTrue(lease.verify(arm).verified, arm)
        self.assertEqual(_FakeServer.starts, 1)
        self.assertEqual(_FakeServer.stops, 1)
        self.assertEqual(lease.record.server_starts, 1)
        self.assertEqual(lease.record.arms_served, 4)

    def test_the_weights_are_re_checked_before_every_arm(self) -> None:
        # Not once per lease: a server verified twelve slices ago is a server
        # whose identity is twelve slices old.
        transport = _transport(_HEALTHY)
        lease = ModelServerLease(
            _Manifest(),
            self.evidence,
            server_factory=_FakeServer,
            transport=transport,
        )
        with lease:
            lease.verify("first")
            lease.verify("second")
        props_calls = [item for item in transport.calls if item[1] == "/props"]
        self.assertEqual(len(props_calls), 2)

    def test_a_different_model_is_a_mismatch_not_a_warning(self) -> None:
        responses = dict(_HEALTHY)
        responses["/props"] = (200, {"model_path": "/models/something-else.gguf"})
        with self._lease(responses) as lease:
            verification = lease.verify("sandbox")
        self.assertFalse(verification.verified)
        self.assertEqual(
            [item.field for item in verification.mismatches], ["model_path"]
        )
        self.assertIn("something-else", verification.detail)
        self.assertEqual(lease.record.arms_served, 0)
        self.assertTrue(lease.record.fallbacks)

    def test_a_wrong_alias_is_refused_too(self) -> None:
        responses = dict(_HEALTHY)
        responses["/v1/models"] = (200, {"data": [{"id": "some-other-model"}]})
        with self._lease(responses) as lease:
            self.assertFalse(lease.verify("sandbox").verified)

    def test_an_unhealthy_server_is_never_leased(self) -> None:
        responses = dict(_HEALTHY)
        responses["/health"] = (503, {})
        with self._lease(responses) as lease:
            self.assertFalse(lease.verify("sandbox").verified)

    def test_a_server_that_cannot_be_identified_is_not_leased(self) -> None:
        """Unavailable is not a pass.

        A build serving neither `/props` nor `/v1/models` gives nothing to
        check the weights against. That falls back to a cold start -- the
        behaviour that existed before the lease -- rather than reusing a
        process on the strength of it answering `/health`.
        """

        responses = {"/health": (200, {})}
        with self._lease(responses) as lease:
            verification = lease.verify("sandbox")
        self.assertFalse(verification.verified)
        self.assertIn("props", verification.checks_unavailable)
        self.assertIn("model_alias", verification.checks_unavailable)
        self.assertEqual(verification.mismatches, [])

    def test_verified_names_what_was_actually_checked(self) -> None:
        with self._lease(_HEALTHY) as lease:
            verification = lease.verify("sandbox")
        self.assertTrue(verification.verified)
        self.assertIn("health", verification.checks_passed)
        self.assertIn("model_path", verification.checks_passed)
        self.assertIn("model_alias", verification.checks_passed)

    def test_the_cache_is_erased_between_arms_and_recorded(self) -> None:
        transport = _transport(_HEALTHY)
        lease = ModelServerLease(
            _Manifest(),
            self.evidence,
            server_factory=_FakeServer,
            transport=transport,
        )
        with lease:
            reset = lease.reset_slots("sandbox")
        self.assertTrue(reset.performed)
        self.assertIn(("POST", "/slots?action=erase"), transport.calls)
        self.assertEqual(lease.record.resets[0].arm, "sandbox")

    def test_a_refused_erase_is_recorded_rather_than_assumed(self) -> None:
        responses = dict(_HEALTHY)
        responses["/slots?action=erase"] = (501, {})
        with self._lease(responses) as lease:
            reset = lease.reset_slots("sandbox")
        self.assertFalse(reset.performed)
        self.assertIn("may reuse a prefix", reset.detail)

    def test_a_transport_failure_is_a_failed_check_not_a_crash(self) -> None:
        def broken(path: str, method: str = "GET", body: dict | None = None):
            raise OSError("connection refused")

        lease = ModelServerLease(
            _Manifest(),
            self.evidence,
            server_factory=_FakeServer,
            transport=broken,
        )
        with lease:
            self.assertFalse(lease.verify("sandbox").verified)

    def test_the_lease_writes_its_own_evidence(self) -> None:
        with self._lease(_HEALTHY) as lease:
            lease.verify("sandbox")
            lease.reset_slots("sandbox")
        record = json.loads(
            (self.evidence / "model-server-lease.json").read_text(encoding="utf-8")
        )
        self.assertEqual(record["server_starts"], 1)
        self.assertEqual(len(record["verifications"]), 1)
        self.assertEqual(len(record["resets"]), 1)


if __name__ == "__main__":
    unittest.main()
