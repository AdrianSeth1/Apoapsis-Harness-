"""Slice 5A task 4: resolved native context settings and per-call decomposition.

Two Slice 5C items are closed here, and both are decision functions over text
somebody else produced, so both are tested on fixed inputs rather than through a
live session.

The tests that matter most are the negative ones. `parse_native_context` and
`native_context_pin_from_resolved` exist to stop an unobserved threshold from
being recorded as an observed one, and `flag_anomalies` exists to stop an
unexplained call from acquiring a plausible explanation. A suite that only
proved the happy paths would leave exactly the hole these were written to close.
"""

from __future__ import annotations

import json
import unittest

from apoapsis.workcell.call_decomposition import (
    ANOMALY_RATIO,
    CallExplanation,
    InternalCall,
    ResidualStatus,
    decompose_invocation,
    flag_anomalies,
    flag_residual_anomalies,
)
from apoapsis.workcell.pin_capture import (
    GoverningTerm,
    PinCaptureError,
    cli_native_context_argv,
    cli_threshold_ladder_argv,
    native_context_pin_from_resolved,
    parse_native_context,
    parse_threshold_ladder,
)
from apoapsis.workcell.pins import NativeContextPin


def _field(value, *, source="settings:context.autoCompactThreshold", resolved=True):
    return {"value": value, "source": source, "resolved": resolved}


def _payload(**overrides):
    payload = {
        "auto_compact_threshold": _field(0.7),
        "max_recent_files_to_retain": _field(9),
        "max_recent_images_to_retain": _field(2),
    }
    payload.update(overrides)
    return json.dumps(payload)


class ResolvedNativeContextTests(unittest.TestCase):
    def test_resolved_settings_produce_an_observed_pin(self) -> None:
        resolved = parse_native_context(_payload(), source="test")
        self.assertTrue(resolved.fully_resolved)
        pin = native_context_pin_from_resolved(resolved)
        self.assertTrue(pin.resolved_from_cli)
        self.assertEqual(pin.auto_compact_threshold, 0.7)
        self.assertEqual(pin.max_recent_files_to_retain, 9)

    def test_the_observed_value_overrides_this_models_default(self) -> None:
        """0.85 is an assumption about the CLI until the CLI contradicts it.

        Slice 5C ran with a settings file that wrote no `context` block at all,
        so the pin's 0.85 was never compared to anything. If the CLI answers
        0.7, the pin has to say 0.7 -- silently keeping 0.85 here would make the
        capture worse than useless, because it would carry the authority of an
        observation while reporting a belief.
        """

        resolved = parse_native_context(_payload(), source="test")
        pin = native_context_pin_from_resolved(resolved)
        self.assertNotEqual(pin.auto_compact_threshold, NativeContextPin().auto_compact_threshold)

    def test_an_unresolved_field_refuses_to_claim_resolution(self) -> None:
        resolved = parse_native_context(
            _payload(
                auto_compact_threshold=_field(
                    None, source="unresolved: no matching default export", resolved=False
                )
            ),
            source="test",
        )
        self.assertFalse(resolved.fully_resolved)
        self.assertEqual(resolved.unresolved_fields(), ["auto_compact_threshold"])

    def test_an_unresolved_capture_degrades_to_defaults_not_to_resolved(self) -> None:
        """The ADR 0069 shape: not checked, never an implicit all-clear.

        The failure being guarded against is subtle and would be easy to ship:
        two of the three fields resolve, the pin is built from what was
        available, `resolved_from_cli` is set because "the capture ran", and the
        threshold the run actually compacted against is still unobserved while
        the manifest now asserts it was measured.
        """

        resolved = parse_native_context(
            _payload(
                max_recent_files_to_retain=_field(None, source="unresolved", resolved=False)
            ),
            source="test",
        )
        pin = native_context_pin_from_resolved(resolved)
        self.assertFalse(pin.resolved_from_cli)
        self.assertEqual(pin, NativeContextPin())

    def test_a_partial_answer_is_refused_rather_than_defaulted(self) -> None:
        payload = json.loads(_payload())
        del payload["max_recent_images_to_retain"]
        with self.assertRaises(PinCaptureError) as caught:
            parse_native_context(json.dumps(payload), source="test")
        self.assertIn("max_recent_images_to_retain", str(caught.exception))

    def test_empty_and_unparseable_output_raise(self) -> None:
        for stdout in ("", "   \n", "not json at all"):
            with self.assertRaises(PinCaptureError):
                parse_native_context(stdout, source="test")

    def test_a_non_numeric_value_is_refused(self) -> None:
        with self.assertRaises(PinCaptureError):
            parse_native_context(
                _payload(auto_compact_threshold=_field("0.85")), source="test"
            )

    def test_only_the_last_line_is_parsed(self) -> None:
        """Node writes warnings to stdout ahead of the payload."""

        noisy = "ExperimentalWarning: something\n" + _payload()
        self.assertTrue(parse_native_context(noisy, source="test").fully_resolved)

    def test_the_argv_substitutes_both_modules_and_stays_a_module(self) -> None:
        argv = cli_native_context_argv(
            settings_module="/bundle/settings.js",
            defaults_module="/bundle/defaults.js",
            workspace="/workspace",
        )
        self.assertEqual(argv[0], "node")
        self.assertIn("--input-type=module", argv)
        script = argv[argv.index("-e") + 1]
        self.assertIn("/bundle/settings.js", script)
        self.assertIn("/bundle/defaults.js", script)
        self.assertNotIn("__SETTINGS_MODULE__", script)
        self.assertNotIn("__DEFAULTS_MODULE__", script)
        self.assertEqual(argv[-1], "/workspace")


#: The exact bytes `computeThresholds` printed when executed inside
#: `apoapsis-qwen-workcell:0.21.1` with `--network none`. Kept verbatim so the
#: fixture is a transcript rather than a construction.
_LADDER_STDOUT = json.dumps(
    {
        "window": 65_536,
        "ladder": {
            "warn": 12_536,
            "auto": 32_536,
            "hard": 42_536,
            "effective_window": 45_536,
        },
        "builtin_pct_probe": {
            "window": 1_000_000,
            "auto": 850_000,
            "effective_window": 980_000,
        },
        "buffer_probe": {"auto": 32_536, "effective_window": 45_536},
    }
)

_CHUNK_SHA = "634214ecb16ef3ab8e6e4046413c965606fd7c7c1194f6db93cd707cb5381c5c"


def _ladder(stdout=None, **kwargs):
    return parse_threshold_ladder(
        stdout if stdout is not None else _LADDER_STDOUT,
        source_chunk_sha256=_CHUNK_SHA,
        source="test",
        **kwargs,
    )


class ThresholdLadderTests(unittest.TestCase):
    """The ladder the pinned CLI computes, measured from its own function."""

    def test_the_effective_trigger_is_not_the_percentage(self) -> None:
        """The finding, stated as an assertion.

        0.85 x 65,536 is 55,706. The CLI compacts at 32,536. Any code that
        multiplies a percentage by a window to predict a trigger is wrong at
        this window, and was wrong for the whole of Slice 5C.
        """

        ladder = _ladder()
        self.assertEqual(ladder.auto_tokens, 32_536)
        self.assertNotEqual(ladder.auto_tokens, 0.85 * 65_536)
        self.assertAlmostEqual(ladder.effective_ratio, 0.4965, places=4)
        self.assertAlmostEqual(ladder.percentage_overstates_trigger_by, 1.7121, places=4)

    def test_the_governing_term_is_the_absolute_ceiling(self) -> None:
        self.assertIs(_ladder().governing_term, GoverningTerm.ABSOLUTE_CEILING)

    def test_the_constants_are_derived_by_measurement(self) -> None:
        ladder = _ladder()
        self.assertEqual(ladder.builtin_pct, 0.85)
        self.assertEqual(ladder.summary_reserve_tokens, 20_000)
        self.assertEqual(ladder.autocompact_buffer_tokens, 13_000)
        self.assertEqual(ladder.effective_window_tokens, 45_536)

    def test_the_whole_ladder_is_carried(self) -> None:
        ladder = _ladder()
        self.assertEqual(
            (ladder.warn_tokens, ladder.auto_tokens, ladder.hard_tokens),
            (12_536, 32_536, 42_536),
        )

    def test_an_unset_configured_pct_is_none_not_the_builtin(self) -> None:
        """Unset is a fact about the run, not a value to be filled in."""

        self.assertIsNone(_ladder().configured_pct)
        self.assertEqual(_ladder().builtin_pct, 0.85)

    def test_the_source_chunk_hash_proves_which_algorithm_answered(self) -> None:
        self.assertEqual(_ladder().source_chunk_sha256, _CHUNK_SHA)

    def test_a_proportional_window_reports_the_proportional_term(self) -> None:
        """At a wide enough window the percentage does describe the trigger."""

        wide = json.loads(_LADDER_STDOUT)
        wide["window"] = 1_000_000
        wide["ladder"] = {
            "warn": 830_000,
            "auto": 850_000,
            "hard": 967_000,
            "effective_window": 980_000,
        }
        ladder = _ladder(json.dumps(wide))
        self.assertIs(ladder.governing_term, GoverningTerm.PROPORTIONAL)
        self.assertAlmostEqual(ladder.percentage_overstates_trigger_by, 1.0, places=6)

    def test_a_probe_that_did_not_land_proportionally_is_refused(self) -> None:
        """Rather than reporting a percentage it did not measure."""

        broken = json.loads(_LADDER_STDOUT)
        broken["builtin_pct_probe"] = {
            "window": 1_000_000,
            "auto": 980_000,
            "effective_window": 980_000,
        }
        with self.assertRaises(PinCaptureError):
            _ladder(json.dumps(broken))

    def test_partial_and_unparseable_probes_raise(self) -> None:
        missing = json.loads(_LADDER_STDOUT)
        del missing["buffer_probe"]
        for stdout in ("", "not json", json.dumps(missing)):
            with self.assertRaises(PinCaptureError):
                _ladder(stdout)

    def test_the_argv_executes_the_cli_function(self) -> None:
        argv = cli_threshold_ladder_argv(
            thresholds_module="/bundle/chunk.js", context_window=65_536
        )
        script = argv[argv.index("-e") + 1]
        self.assertIn("computeThresholds", script)
        self.assertIn("/bundle/chunk.js", script)
        self.assertNotIn("__THRESHOLDS_MODULE__", script)
        self.assertEqual(argv[-1], "65536")


def _usage_event(input_tokens, *, output=10, cached=None, stop=None):
    usage = {"input_tokens": input_tokens, "output_tokens": output}
    if cached is not None:
        usage["cache_read_input_tokens"] = cached
    message = {"usage": usage}
    if stop is not None:
        message["stop_reason"] = stop
    return {"type": "assistant", "message": message}


class CallDecompositionTests(unittest.TestCase):
    def test_calls_are_ordered_and_indexed(self) -> None:
        record = {"events": [_usage_event(22_431), _usage_event(33_400), _usage_event(33_462)]}
        decomposed = decompose_invocation(record)
        self.assertEqual([call.index for call in decomposed.calls], [0, 1, 2])
        self.assertEqual(decomposed.first_call.input_tokens, 22_431)
        self.assertEqual(decomposed.total_input_tokens, 89_293)

    def test_the_cli_cache_spelling_is_read(self) -> None:
        """`cache_read_input_tokens`, not `cached_input_tokens`.

        Reading the wrong spelling is what made the live Slice 5C stage report
        NOT_MEASURABLE on a server that was reporting cache telemetry all along.
        """

        decomposed = decompose_invocation({"events": [_usage_event(22_431, cached=19_742)]})
        self.assertEqual(decomposed.calls[0].cached_input_tokens, 19_742)

    def test_a_call_whose_input_deviates_is_flagged_unexplained(self) -> None:
        record = {
            "events": [
                _usage_event(22_433),
                _usage_event(53_397),
                _usage_event(33_431),
                _usage_event(33_431),
            ]
        }
        decomposed = decompose_invocation(record)
        self.assertEqual(len(decomposed.unexplained_anomalies), 1)
        anomaly = decomposed.unexplained_anomalies[0]
        self.assertEqual(anomaly.index, 1)
        self.assertGreater(anomaly.ratio, ANOMALY_RATIO)
        self.assertIs(anomaly.explanation, CallExplanation.UNEXPLAINED)

    def test_a_call_after_compaction_is_explained_not_flagged_unexplained(self) -> None:
        record = {
            "events": [
                _usage_event(22_433),
                _usage_event(33_431),
                _usage_event(33_431),
                {"type": "compaction"},
                _usage_event(53_397),
            ]
        }
        decomposed = decompose_invocation(record)
        self.assertEqual(decomposed.unexplained_anomalies, [])
        self.assertIs(
            decomposed.anomalies[0].explanation, CallExplanation.FOLLOWS_COMPACTION
        )

    def test_a_retry_after_a_ceiling_stop_is_explained(self) -> None:
        record = {
            "events": [
                _usage_event(22_433),
                _usage_event(33_431),
                _usage_event(33_431, stop="length"),
                _usage_event(53_397),
            ]
        }
        decomposed = decompose_invocation(record)
        self.assertEqual(decomposed.unexplained_anomalies, [])
        self.assertIs(
            decomposed.anomalies[0].explanation, CallExplanation.FOLLOWS_CEILING_STOP
        )

    def test_the_first_call_never_moves_the_cohort_baseline(self) -> None:
        """Index 0 carries a different prompt and is excluded from the median.

        Including it is a variant of the error the Slice 5C recomputation had to
        undo: comparing calls whose prompts differ by construction, and being
        right only by coincidence.
        """

        calls = [
            InternalCall(index=0, input_tokens=200_000),
            InternalCall(index=1, input_tokens=33_431),
            InternalCall(index=2, input_tokens=33_431),
        ]
        self.assertEqual(flag_anomalies(calls), [])

    def test_a_single_continuation_reports_no_anomaly(self) -> None:
        calls = [
            InternalCall(index=0, input_tokens=22_431),
            InternalCall(index=1, input_tokens=99_999),
        ]
        self.assertEqual(flag_anomalies(calls), [])

    def test_small_counts_are_not_flagged_on_ratio_alone(self) -> None:
        calls = [
            InternalCall(index=0, input_tokens=20),
            InternalCall(index=1, input_tokens=4),
            InternalCall(index=2, input_tokens=12),
            InternalCall(index=3, input_tokens=4),
        ]
        self.assertEqual(flag_anomalies(calls), [])

    def test_an_unusually_small_call_is_also_flagged(self) -> None:
        record = {
            "events": [
                _usage_event(22_433),
                _usage_event(33_431),
                _usage_event(33_431),
                _usage_event(2_000),
            ]
        }
        decomposed = decompose_invocation(record)
        self.assertEqual([item.index for item in decomposed.anomalies], [3])
        self.assertLess(decomposed.anomalies[0].ratio, 1.0)

    def test_events_without_usage_are_skipped(self) -> None:
        record = {
            "events": [
                {"type": "system"},
                {"type": "assistant", "message": {"content": []}},
                _usage_event(22_431),
                "not a dict",
            ]
        }
        self.assertEqual(len(decompose_invocation(record).calls), 1)

    def test_an_empty_record_decomposes_to_nothing(self) -> None:
        decomposed = decompose_invocation({})
        self.assertEqual(decomposed.calls, [])
        self.assertIsNone(decomposed.first_call)
        self.assertEqual(decomposed.anomalies, [])
        self.assertIsNone(decomposed.residual)
        self.assertIs(decomposed.residual_status, ResidualStatus.NO_AGGREGATE)


def _result_event(input_tokens, output, cached):
    return {
        "type": "result",
        "usage": {
            "input_tokens": input_tokens,
            "output_tokens": output,
            "cache_read_input_tokens": cached,
        },
    }


#: The retained stage-7 evidence, transcribed from
#: `.apoapsis-eval/slice5c-2026-07-30/evidence/stage7-*.json`. Each invocation
#: exposed exactly one usage-bearing `assistant` message plus a `result`
#: aggregate.
_STAGE7 = {
    "stable-0": ((22_431, 19, 19_742), (33_427, 115, 27_535)),
    "stable-1": ((22_431, 23, 21_915), (33_427, 218, 29_708)),
    "stable-2": ((22_431, 31, 21_915), (33_427, 224, 29_708)),
    "perturbed-0": ((22_433, 20, 19_742), (33_431, 195, 27_535)),
    "perturbed-1": ((22_433, 24, 19_742), (53_397, 475, 26_487)),
    "perturbed-2": ((22_433, 21, 19_742), (33_431, 192, 27_535)),
}


def _stage7_decompositions():
    return {
        label: decompose_invocation(
            {
                "events": [
                    _usage_event(msg[0], output=msg[1], cached=msg[2]),
                    _result_event(*res),
                ]
            }
        )
        for label, (msg, res) in _STAGE7.items()
    }


class RetainedSlice5CEvidenceTests(unittest.TestCase):
    """Regression fixtures transcribed from the retained Slice 5C evidence.

    The record described 53,397 as a "second internal call". The evidence shows
    it is the `result` aggregate, and that the invocation exposed one message at
    22,433. These tests hold the corrected reading in place.
    """

    def test_the_aggregate_is_not_counted_as_a_call(self) -> None:
        """Otherwise a total is compared against its own component."""

        decomposed = _stage7_decompositions()["perturbed-1"]
        self.assertEqual(len(decomposed.calls), 1)
        self.assertEqual(decomposed.calls[0].input_tokens, 22_433)
        self.assertEqual(decomposed.aggregate.input_tokens, 53_397)

    def test_perturbed_1_residual_matches_the_owner_verified_figures(self) -> None:
        residual = _stage7_decompositions()["perturbed-1"].residual
        self.assertEqual(residual.input_tokens, 30_964)
        self.assertEqual(residual.output_tokens, 451)
        self.assertEqual(residual.cached_input_tokens, 6_745)

    def test_every_stage7_invocation_carries_an_unattributed_residual(self) -> None:
        """The residual is structural, not unique to the outlier.

        Five of six sit near 10,997 input tokens. An instrument that treated a
        residual as an anomaly per se would fire on all six.
        """

        for label, decomposed in _stage7_decompositions().items():
            with self.subTest(label=label):
                self.assertIs(
                    decomposed.residual_status,
                    ResidualStatus.UNATTRIBUTED_RESIDUAL,
                )

    def test_only_perturbed_1_deviates_from_the_residual_cohort(self) -> None:
        anomalies = flag_residual_anomalies(_stage7_decompositions())
        self.assertEqual([item.label for item in anomalies], ["perturbed-1"])
        self.assertEqual(anomalies[0].residual_input_tokens, 30_964)
        self.assertAlmostEqual(anomalies[0].cohort_median, 10_997.0, places=0)
        self.assertGreater(anomalies[0].ratio, ANOMALY_RATIO)

    def test_the_measured_cache_benefit_is_unaffected(self) -> None:
        """2,173 tokens, on the first exposed message, as recorded.

        The correction is to the residual's classification, not to the cache
        result: that was measured on the exposed message and the exposed
        message is unchanged.
        """

        decompositions = _stage7_decompositions()
        stable = [
            decompositions[f"stable-{i}"].calls[0].cached_input_tokens for i in range(3)
        ]
        perturbed = [
            decompositions[f"perturbed-{i}"].calls[0].cached_input_tokens
            for i in range(3)
        ]
        self.assertEqual(max(stable) - max(perturbed), 2_173)

    def test_over_attribution_is_reported_rather_than_clamped(self) -> None:
        decomposed = decompose_invocation(
            {"events": [_usage_event(50_000), _result_event(20_000, 5, 5)]}
        )
        self.assertIs(decomposed.residual_status, ResidualStatus.OVER_ATTRIBUTED)

    def test_a_cohort_smaller_than_three_reports_nothing(self) -> None:
        subset = {
            label: item
            for label, item in _stage7_decompositions().items()
            if label in ("stable-0", "perturbed-1")
        }
        self.assertEqual(flag_residual_anomalies(subset), [])


if __name__ == "__main__":
    unittest.main()
