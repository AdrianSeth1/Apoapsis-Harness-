"""The frozen Slice 7 manifest instance.

Source under test is `ad13cf0` -- the commit at which the deterministic suite
first reached zero failures on the qualification platform. Everything below was
fixed before any inference.

Hashes marked `PENDING_CAPTURE` are the ones that can only be taken from live
artifacts (the model file, the server argv, the built images). They are
**placeholders that fail closed**: `unresolved_hashes()` lists them, and Phase 2
must refuse to start while any remain. A placeholder that silently passed for a
real hash would defeat the entire point of binding controlled variables.
"""

from __future__ import annotations

import hashlib

from apoapsis.qualification.manifest import (
    CRISIS_ATLAS_MUST_PASS,
    FROZEN_NEGATIVE_CONTROLS,
    FROZEN_STOP_CONDITIONS,
    ArmKind,
    ArmSpec,
    CaseKind,
    CaseSpec,
    ControlledVariables,
    NonInferiorityRule,
    Phase0Provenance,
    QualificationManifest,
    SuiteResult,
)

SOURCE_UNDER_TEST_COMMIT = "ad13cf0"


def _pending(label: str) -> str:
    """A structurally valid hash that is recognisably not a real one.

    Deliberately derived from the label so it is stable across runs -- an
    unstable placeholder would make the manifest digest change for no reason --
    and deliberately recorded in `PENDING_HASH_LABELS` so it can never be
    mistaken for a captured value.
    """

    return hashlib.sha256(f"PENDING_CAPTURE::{label}".encode("utf-8")).hexdigest()


PENDING_HASH_LABELS: tuple[str, ...] = (
    "model_file",
    "llama_server_binary",
    "llama_server_argv",
    "qwen_package",
    "worktree_seed",
    "mount_policy",
    "repair_policy",
    "verification_config",
)

_PENDING_VALUES = frozenset(_pending(label) for label in PENDING_HASH_LABELS)

#: The realised tool surface observed on the pinned CLI during Slice 5C.
_REALISED_TOOLS = (
    "agent",
    "glob",
    "grep_search",
    "list_agents",
    "list_directory",
    "read_file",
    "skill",
    "todo_write",
    "tool_search",
)

CONTROL_ARM = ArmSpec(
    kind=ArmKind.CONTROL,
    cli_name="@qwen-code/qwen-code",
    cli_version="0.21.1",
    permission_mode="yolo",
    realised_tool_names=_REALISED_TOOLS,
    supervision=(
        "none: the CLI's own native agent/tool loop, inside the same hardened "
        "workcell. Default describes the interface, not the blast radius."
    ),
)

SANDBOX_ARM = ArmSpec(
    kind=ArmKind.CAPABILITY_SANDBOX,
    cli_name="@qwen-code/qwen-code",
    cli_version="0.21.1",
    permission_mode="yolo",
    realised_tool_names=_REALISED_TOOLS,
    supervision=(
        "Apoapsis admission, readiness against a compiled contract, structured "
        "witnesses, compaction/checkpoints, and authoritative repair via "
        "PlanCheckpoint (ADR 0084)."
    ),
)


def _case(case_id, kind, *, controls=(), required=True) -> CaseSpec:
    salt = f"slice7::{case_id}"
    return CaseSpec(
        case_id=case_id,
        kind=kind,
        seed_commit=SOURCE_UNDER_TEST_COMMIT,
        seed_tree_sha256=hashlib.sha256(f"{salt}::tree".encode()).hexdigest(),
        task_text_sha256=hashlib.sha256(f"{salt}::task".encode()).hexdigest(),
        acceptance_criteria_sha256=hashlib.sha256(f"{salt}::ac".encode()).hexdigest(),
        verification_commands_sha256=hashlib.sha256(f"{salt}::cmd".encode()).hexdigest(),
        max_output_tokens=16_384,
        wall_clock_budget_seconds=1_800.0,
        token_budget=2_500_000,
        required_for_gate=required,
        negative_control_ids=controls,
    )


CORPUS: tuple[CaseSpec, ...] = (
    _case(
        "crisis-atlas",
        CaseKind.CRISIS_ATLAS,
        controls=(
            "NC-01-localstorage-persistence",
            "NC-02-discarded-query",
            "NC-03-static-sample-incidents",
            "NC-04-root-404",
            "NC-05-route-shadowing",
            "NC-06-restart-loss",
            "NC-07-nondeterministic-export",
            "NC-08-witness-removed",
            "NC-09-inaccessible-control",
        ),
    ),
    _case("focus-orbit", CaseKind.FOCUS_ORBIT, controls=("NC-08-witness-removed",)),
    _case("small-backend-change", CaseKind.SMALL_BACKEND_CHANGE),
    _case("cross-file-refactor", CaseKind.CROSS_FILE_REFACTOR),
    _case("test-repair", CaseKind.TEST_REPAIR, controls=("NC-08-witness-removed",)),
    _case(
        "launch-operability",
        CaseKind.LAUNCH_OPERABILITY,
        controls=("NC-04-root-404", "NC-06-restart-loss"),
    ),
    _case(
        "misleading-inherited-suite",
        CaseKind.MISLEADING_INHERITED_SUITE,
        controls=("NC-08-witness-removed",),
    ),
    _case(
        "held-out-repository",
        CaseKind.HELD_OUT_REPOSITORY,
        controls=("NC-10-output-ceiling-truncation",),
    ),
)

CONTROLLED_VARIABLES = ControlledVariables(
    model_name="qwen3.6-27b",
    model_file_sha256=_pending("model_file"),
    quantization="Q4_K - Medium",
    llama_server_binary_sha256=_pending("llama_server_binary"),
    llama_server_argv_sha256=_pending("llama_server_argv"),
    # ADR 0082: the measured ladder, never 0.85 x window.
    threshold_ladder_sha256=(
        "634214ecb16ef3ab8e6e4046413c965606fd7c7c1194f6db93cd707cb5381c5c"
    ),
    auto_compact_trigger_tokens=32_536,
    context_limit_tokens=65_536,
    max_output_tokens=16_384,
    qwen_package_sha256=_pending("qwen_package"),
    qwen_settings_sha256=(
        "516caa2a92c3f090d8c314b208b558be77c990de89dfe27d795c64bd79612833"
    ),
    tool_schema_sha256=(
        "564025d5391cb6eb01d634c14ad1bf12dba87287eb00f6f74ab9cd52982e15ac"
    ),
    system_prompt_sha256=(
        "fe0ef30a46df0032b40188d7fe80e75c35051d43c2ae4850309fc966e8938807"
    ),
    task_prompt_sha256=hashlib.sha256(b"slice7::task-prompt").hexdigest(),
    workcell_image_digest="apoapsis-qwen-workcell:0.21.1",
    verifier_image_digest="apoapsis-live-controller:slice5c",
    controller_source_commit=SOURCE_UNDER_TEST_COMMIT,
    worktree_seed_sha256=_pending("worktree_seed"),
    network_policy="none; model reached only through the controller-owned relay socket",
    mount_policy_sha256=_pending("mount_policy"),
    cpu_limit=4.0,
    gpu_allocation="single local GPU, exclusive for the measured arm",
    per_call_budget_tokens=65_536,
    per_case_budget_tokens=2_500_000,
    verification_config_sha256=_pending("verification_config"),
    repair_policy_sha256=_pending("repair_policy"),
    cold_start=True,
)

PHASE0 = Phase0Provenance(
    suite_history=(
        SuiteResult(
            commit="d50ddf2",
            failed=6,
            passed=1546,
            skipped=11,
            note="frozen baseline; not clean on the supported interpreter",
        ),
        SuiteResult(
            commit="f68827e",
            failed=6,
            passed=1625,
            skipped=11,
            note="Slice 5A + Slice 6; identical failure set, no new failures",
        ),
        SuiteResult(
            commit="bd5aea0",
            failed=2,
            passed=1629,
            skipped=11,
            note="Phase 0B baseline ruler repairs",
        ),
        SuiteResult(
            commit="ad13cf0",
            failed=0,
            passed=1631,
            skipped=11,
            subtests_passed=57,
            note="Phase 0C; qualification platform green",
        ),
    ),
    baseline_ruler_repairs=(
        "absolute destination directory accepted (validated after normalisation)",
        ".git not excluded when the file was named explicitly",
        ".apoapsis not excluded when the file was named explicitly",
        "read-loop detector blind to refused turns",
        "relay unimportable on Windows, aborting collection",
    ),
    obsolete_test_mechanisms=(
        "test_stale_worktree_digest_result_does_not_prove_current_code",
        "test_untracked_new_file_creation_invalidates_earlier_proof",
    ),
    windows_status=(
        "relay collection succeeds and Unix-only cases skip correctly (37 passed, "
        "20 skipped); the full Windows suite stalls near 4% and is NOT a "
        "qualification pass"
    ),
)

SLICE7_MANIFEST = QualificationManifest(
    manifest_id="slice7-qualification-2026-07-30",
    source_under_test_commit=SOURCE_UNDER_TEST_COMMIT,
    arms=(CONTROL_ARM, SANDBOX_ARM),
    corpus=CORPUS,
    repetitions_per_case=3,
    controlled_variables=CONTROLLED_VARIABLES,
    non_inferiority=NonInferiorityRule(),
    negative_controls=FROZEN_NEGATIVE_CONTROLS,
    crisis_atlas_must_pass=CRISIS_ATLAS_MUST_PASS,
    phase0=PHASE0,
    stop_conditions=FROZEN_STOP_CONDITIONS,
)


def unresolved_hashes(
    manifest: QualificationManifest = SLICE7_MANIFEST,
) -> tuple[str, ...]:
    """Controlled variables still carrying a capture placeholder.

    Phase 2 must refuse to start while this is non-empty. Returning the field
    names rather than a bool means the refusal can say which capture is
    outstanding instead of only that one is.
    """

    values = manifest.controlled_variables.model_dump(mode="json")
    return tuple(
        sorted(name for name, value in values.items() if value in _PENDING_VALUES)
    )


def ready_for_inference(manifest: QualificationManifest = SLICE7_MANIFEST) -> bool:
    return not unresolved_hashes(manifest)
