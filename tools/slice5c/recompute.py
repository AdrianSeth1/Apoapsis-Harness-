"""Recompute the Slice 5C summary from raw evidence. No model calls.

Two corrections are applied to what the live run wrote, and both are
recomputations of existing raw bytes rather than new measurements:

1. **Cached input was normalised too late.** Stage 7 read
   `cached_input_tokens` straight off the provider message, but the CLI spells
   it `cache_read_input_tokens`. The adapter already knows both spellings;
   Stage 7 simply was not using it. Reading through `_flatten_usage` turns a
   false `NOT_MEASURABLE` into a measured result.

2. **Stage 5's utilisation figure was meaningless.** `196,639 / 65,536 =
   3.0005` was reported as context utilisation. It is not: the reported input
   total is aggregate usage across every internal call Qwen made during the
   turn, not one prompt's occupancy of the window. The ratio is dropped. The
   three observed native compaction events stand on their own as direct
   evidence and need no ratio to support them.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, "/opt/apoapsis/src")

from apoapsis.workcell.events import _flatten_usage  # noqa: E402

EV = Path(sys.argv[1] if len(sys.argv) > 1 else "/ev/evidence")


def first_provider_usage(record: dict) -> dict:
    """The FIRST provider message with real usage, normalised.

    First, deliberately. A single `qwen -p` invocation makes several internal
    calls, and only the first one carries the prefix this control perturbs --
    its `input_tokens` is identical across every call in an arm (22,431 stable
    / 22,433 perturbed), which is what makes the cached-input comparison
    controlled. Later calls in the same invocation have different prompts
    entirely, so taking a max across all of them compares two different
    things and only looked right by coincidence.
    """

    for item in record.get("events", []):
        for block in (item.get("usage"), (item.get("message") or {}).get("usage")):
            if not isinstance(block, dict) or not block:
                continue
            flat = _flatten_usage(block, finish_reason=item.get("stop_reason"))
            if flat.get("input_tokens"):
                return {k: v for k, v in flat.items() if v is not None}
    return {}


def all_provider_usage(record: dict) -> list[dict]:
    """Every usage-bearing message, kept so the record is auditable."""

    seen = []
    for item in record.get("events", []):
        for block in (item.get("usage"), (item.get("message") or {}).get("usage")):
            if isinstance(block, dict) and block:
                flat = _flatten_usage(block)
                if flat.get("input_tokens"):
                    seen.append({k: v for k, v in flat.items() if v is not None})
    return seen


def load(name: str):
    path = EV / name
    return json.loads(path.read_text()) if path.exists() else None


def main() -> int:
    summary = load("summary.json") or {}
    stages = summary.setdefault("stages", {})

    # --- stage 5: drop the utilisation ratio --------------------------
    stage5 = stages.get("5_native_compaction")
    if stage5:
        for entry in stage5.get("log", []):
            entry.pop("utilisation", None)
            entry["note"] = (
                "input_tokens is aggregate usage across Qwen's internal calls "
                "for this turn, not context occupancy; no ratio is derived"
            )
        stage5["utilisation_claim_withdrawn"] = (
            "A previously reported 3.0005 'utilisation' divided aggregate turn "
            "usage by the context limit. That number described nothing. The "
            "compaction events below are direct observations and do not depend "
            "on it."
        )

    # --- stage 7: recompute from raw provider usage -------------------
    arms: dict[str, list] = {}
    for arm in ("stable", "perturbed"):
        reads = []
        for index in range(8):
            record = load(f"stage7-{arm}-{index}.json")
            if record is None:
                continue
            reads.append(
                {
                    "index": index,
                    "usage": first_provider_usage(record),
                    "all_provider_messages": all_provider_usage(record),
                    "elapsed_seconds": record.get("elapsed_seconds"),
                    "exit_code": record.get("exit_code"),
                }
            )
        if reads:
            arms[arm] = reads

    if arms:
        def cached(arm):
            return [
                item["usage"].get("cached_input_tokens")
                for item in arms.get(arm, [])
                if isinstance(item["usage"].get("cached_input_tokens"), int)
            ]

        stable, perturbed = cached("stable"), cached("perturbed")
        benefit = max(stable) - max(perturbed) if stable and perturbed else None
        stages["7_cache_control"] = {
            "arms": arms,
            "cached_input_telemetry_present": bool(stable or perturbed),
            "stable_cached_input_tokens": stable,
            "perturbed_cached_input_tokens": perturbed,
            "stable_prefix_benefit_tokens": benefit,
            "measured_on": (
                "the first provider message of each invocation, whose "
                "input_tokens is constant within each arm -- the only "
                "controlled comparison available"
            ),
            "verdict": (
                f"MEASURED: repeated calls on a byte-identical prefix raised "
                f"cached input from {min(stable)} to {max(stable)} while the "
                f"perturbed arm stayed flat at {max(perturbed)}; the "
                f"stable-prefix benefit is {benefit} tokens"
                if benefit is not None
                else "NOT_MEASURABLE"
            ),
            "correction": (
                "recomputed from the raw provider messages through "
                "_flatten_usage; the live run read the trace spelling "
                "`cached_input_tokens` off a CLI that emits "
                "`cache_read_input_tokens` and wrongly concluded the server "
                "reported no cache telemetry"
            ),
        }

    verdict = summary.setdefault("verdict", {})
    if stage5:
        verdict["context_safety"] = stage5.get("verdict")
    if "7_cache_control" in stages:
        verdict["efficiency"] = stages["7_cache_control"]["verdict"]

    (EV / "summary.json").write_text(json.dumps(summary, indent=2))
    print(json.dumps(verdict, indent=2))
    print(json.dumps(stages.get("7_cache_control", {}).get(
        "stable_cached_input_tokens"), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
