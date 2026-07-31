"""The Crisis Atlas pilot manifest and its lock.

The eight-case draft (`cfe7df7`, digest `8c374827…`) stays exactly where it is:
a historical record of a qualification attempt that was correctly refused. This
is a *separate* artifact covering one case, three repetitions and two arms, and
nothing here edits that draft.

Three things this module is built to make impossible.

**Claiming more than one case proves.** `PilotScope` carries the disclaimers as
required booleans rather than prose, so a reader cannot skim past them and a
test can assert them. `broad_non_inferiority_claimed` must be false; so must
`held_out_qualification_claimed`; `default_rollout_prohibited` must be true.
A manifest that says otherwise will not construct.

**Calling a session identity a sampling seed.** Three static audits -- the
provider payload, the server argv, and Qwen's resolved `samplingParams` --
found no seed on any path. `SamplingAudit` records that finding and refuses the
word unless a request field is named. Model sampling is stochastic across the
three repetitions, so comparison is `paired_within_repetition_only`: valid
within a pair, never averaged across pairs into a determinism claim.

**Authorising live inference by accident.** The manifest alone authorises
nothing. `ready_for_inference()` becoming true means every controlled variable
resolves, not that anything may run. Only a `PilotLock` -- written in a
*separate* commit, so it can name the manifest commit without containing it --
authorises the zero-token rehearsal, and even that is not authorisation to
call a model.
"""

from __future__ import annotations

import hashlib
import json
from enum import StrEnum
from typing import Literal

from pydantic import Field, model_validator

from apoapsis.specification.schema import StrictModel

_SHA256 = r"^[0-9a-f]{64}$"
_GIT40 = r"^[0-9a-f]{40}$"
_GIT_SHORT = r"^[0-9a-f]{7,40}$"
_IMAGE_DIGEST = r"^sha256:[0-9a-f]{64}$"

PILOT_MANIFEST_SCHEMA_VERSION = "1.0"
PILOT_LOCK_SCHEMA_VERSION = "1.0"

#: Exactly one case, exactly three repetitions, exactly two arms.
PILOT_CASE_ID = "crisis-atlas"
REQUIRED_REPETITIONS = 3


def canonical_digest(payload: dict) -> str:
    """One stable value over a canonically serialised object."""

    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


class ArmKind(StrEnum):
    #: Unrestricted Qwen CLI, the baseline whose performance must be preserved.
    DEFAULT_QWEN_CONTROL = "default_qwen_control"
    #: Apoapsis Capability Sandbox.
    APOAPSIS_SANDBOX = "apoapsis_sandbox"


class GoverningTerm(StrEnum):
    PROPORTIONAL = "proportional"
    ABSOLUTE_CEILING = "absolute_ceiling"


class PilotScope(StrictModel):
    """What this pilot may and may not be used to claim.

    Booleans with fixed required values rather than a prose paragraph, because
    a paragraph is skimmed and a `Literal[False]` is enforced. Crisis Atlas
    influenced the acceptance rules it is now being used to test, so it is a
    regression benchmark and cannot be held-out evidence however it scores.
    """

    scope: Literal["crisis_atlas_regression_pilot"] = "crisis_atlas_regression_pilot"
    case_ids: tuple[str, ...] = (PILOT_CASE_ID,)
    broad_non_inferiority_claimed: Literal[False] = False
    held_out_qualification_claimed: Literal[False] = False
    default_rollout_prohibited: Literal[True] = True
    #: Eligible only after every live gate passes. Eligibility is not approval.
    optin_slice8_pilot_eligible_after_live_gates: Literal[True] = True
    deferred_corpus_cases: tuple[str, ...] = (
        "focus-orbit",
        "small-backend-change",
        "cross-file-refactor",
        "test-repair",
        "launch-operability",
        "misleading-inherited-suite",
        "held-out-repository",
    )
    statement: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_single_case(self) -> PilotScope:
        if self.case_ids != (PILOT_CASE_ID,):
            raise ValueError(
                f"the pilot covers exactly one case, {PILOT_CASE_ID!r}; "
                f"{list(self.case_ids)} would make it a corpus claim"
            )
        if len(self.deferred_corpus_cases) != 7:
            raise ValueError(
                "seven corpus cases remain deferred; they are deferred, not "
                "deleted, and the count is part of the honest statement"
            )
        return self


class ModelIdentity(StrictModel):
    """The weights, recomputed from the file rather than trusted."""

    absolute_path: str = Field(min_length=1)
    size_bytes: int = Field(gt=0)
    sha256: str = Field(pattern=_SHA256)
    gguf_magic: Literal["GGUF"] = "GGUF"
    gguf_version: int = Field(ge=1)
    tensor_count: int = Field(ge=1)
    metadata_kv_count: int = Field(ge=1)
    model_alias: str = Field(min_length=1)
    quantization: str = Field(min_length=1)
    #: How the digest above was obtained. A recorded hash that was copied
    #: forward from an earlier document is not a measurement.
    digest_source: Literal["recomputed_from_file"] = "recomputed_from_file"


class ServerIdentity(StrictModel):
    """`llama-server`, its build, and the complete ordered argv.

    `argv` is a structured array, never a rendered shell string. A rendered
    command has to be re-parsed to be compared, and quoting differences that
    change nothing look like changes while flag reordering that changes
    prompt-cache behaviour looks like nothing.
    """

    absolute_path: str = Field(min_length=1)
    size_bytes: int = Field(gt=0)
    sha256: str = Field(pattern=_SHA256)
    build_version: str = Field(min_length=1)
    build_commit: str = Field(pattern=_GIT_SHORT)
    built_with: str = Field(min_length=1)
    elf_build_id: str = Field(min_length=1)
    argv: tuple[str, ...] = Field(min_length=3)
    argv_sha256: str = Field(pattern=_SHA256)
    #: Defaults nobody passed but everybody depends on, written down so a
    #: later build changing one is a visible difference rather than a silent
    #: one.
    accepted_implicit_defaults: dict[str, str]
    started_during_this_phase: Literal[False] = False

    @model_validator(mode="after")
    def validate_argv_digest_and_order(self) -> ServerIdentity:
        expected = hashlib.sha256(
            json.dumps(list(self.argv), separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        if expected != self.argv_sha256:
            raise ValueError(
                "argv_sha256 does not match the argv array. The digest is "
                "computed over the ordered list, so reordering flags is a "
                "different experiment even when the set is identical."
            )
        if self.argv[0] != self.absolute_path:
            raise ValueError("argv[0] must be the pinned server path")
        return self


class ThresholdLadder(StrictModel):
    """The compaction ladder the pinned build's own `computeThresholds` returns.

    Bound rather than derived. `0.85 x 65,536` is 55,706; the real trigger is
    32,536, because `computeThresholds` returns the *minimum* of a proportional
    term and an absolute ceiling and at this window the ceiling governs. A
    consumer that multiplies the percentage by the window overstates the
    trigger by 1.71x, which is exactly what happened for the whole of Slice 5C.
    """

    context_window: int = Field(ge=1)
    #: `None` means unset, which is a fact about the run, not a gap to fill.
    configured_pct: float | None = Field(default=None, gt=0.0, le=1.0)
    builtin_pct: float = Field(gt=0.0, le=1.0)
    summary_reserve_tokens: int = Field(ge=0)
    autocompact_buffer_tokens: int = Field(ge=0)
    effective_window_tokens: int = Field(ge=0)
    warn_tokens: int = Field(ge=0)
    auto_tokens: int = Field(ge=0)
    hard_tokens: int = Field(ge=0)
    governing_term: GoverningTerm
    source_chunk_sha256: str = Field(pattern=_SHA256)
    source: str = Field(min_length=1)

    @property
    def effective_auto_ratio(self) -> float:
        return self.auto_tokens / self.context_window

    @model_validator(mode="after")
    def validate_ladder_is_internally_consistent(self) -> ThresholdLadder:
        if not (self.warn_tokens < self.auto_tokens < self.hard_tokens):
            raise ValueError("the ladder must be ordered warn < auto < hard")
        if self.effective_window_tokens != (
            self.context_window - self.summary_reserve_tokens
        ):
            raise ValueError(
                "effective_window must equal window minus the summary reserve"
            )
        proportional = self.builtin_pct * self.context_window
        ceiling = self.effective_window_tokens - self.autocompact_buffer_tokens
        governing = (
            GoverningTerm.PROPORTIONAL
            if proportional <= ceiling
            else GoverningTerm.ABSOLUTE_CEILING
        )
        if governing is not self.governing_term:
            raise ValueError(
                f"governing_term says {self.governing_term} but the numbers "
                f"say {governing}: proportional={proportional:.0f}, "
                f"ceiling={ceiling}"
            )
        if self.auto_tokens != min(int(proportional), ceiling):
            raise ValueError(
                "auto_tokens is not min(proportional, ceiling); this is the "
                "field a naive pct*window prediction gets wrong"
            )
        return self


class QwenIdentity(StrictModel):
    """The coding CLI, as it exists inside the pinned image."""

    image: str = Field(min_length=1)
    image_digest: str = Field(pattern=_IMAGE_DIGEST)
    package_name: str = Field(min_length=1)
    package_version: str = Field(min_length=1)
    entry_point_path: str = Field(min_length=1)
    entry_point_sha256: str = Field(pattern=_SHA256)
    package_metadata_sha256: str = Field(pattern=_SHA256)
    effective_settings_bytes: int = Field(ge=0)
    effective_settings_sha256: str = Field(pattern=_SHA256)
    approval_mode: Literal["yolo"] = "yolo"
    computer_use_enabled: Literal[False] = False
    tool_search_enabled: Literal[False] = False
    expected_tool_names: tuple[str, ...] = Field(min_length=1)
    expected_tool_names_sha256: str = Field(pattern=_SHA256)
    expected_native_tool_count: int = Field(ge=1)
    qwen_home: str = Field(min_length=1)
    #: Where the expected tool surface came from. Static package presence is
    #: explicitly insufficient: Slice 2C found an image whose CLI exposed 57
    #: tools, none of them `write_file`, while the pin declared 13.
    tool_surface_evidence: str = Field(min_length=1)
    #: Live preflight must observe the realised surface again before any
    #: experiment inference. Declared here so the obligation is part of the
    #: manifest rather than a note someone remembers.
    requires_live_preflight_reobservation: Literal[True] = True

    @model_validator(mode="after")
    def validate_tool_digest_and_order(self) -> QwenIdentity:
        if list(self.expected_tool_names) != sorted(self.expected_tool_names):
            raise ValueError("expected_tool_names must be sorted for a stable digest")
        if len(set(self.expected_tool_names)) != len(self.expected_tool_names):
            raise ValueError("expected_tool_names contains duplicates")
        expected = hashlib.sha256(
            json.dumps(list(self.expected_tool_names), separators=(",", ":")).encode(
                "utf-8"
            )
        ).hexdigest()
        if expected != self.expected_tool_names_sha256:
            raise ValueError("expected_tool_names_sha256 does not match the names")
        if len(self.expected_tool_names) != self.expected_native_tool_count:
            raise ValueError(
                "expected_native_tool_count disagrees with the declared names"
            )
        return self


class SamplingAudit(StrictModel):
    """Whether a declared per-repetition seed reaches a provider request.

    It does not. Three static audits, no inference:

    * the Apoapsis provider payload carries `model`, `messages`, `stream` and
      `options{temperature, num_predict, num_ctx}` -- there is no seed field;
    * the canonical `llama-server` argv has no `--seed`;
    * Qwen's resolved generation config has `samplingParams` of
      `{"max_tokens": 16384}`, and the string "seed" occurs nowhere in it.

    So the three repetitions are *repetition identities*, and this type refuses
    to let them be called anything else without evidence. Determinism is not
    invented here, and no seed is added to the request path during the pilot.
    """

    seed_reaches_provider_request: bool
    provider_request_field: str | None = None
    audited_paths: tuple[dict[str, str], ...] = Field(min_length=3)
    model_sampling: Literal["stochastic", "seeded"]
    #: Comparison is valid inside a matched pair, which shares byte-identical
    #: configuration. Across repetitions the runs are independent samples; at
    #: n=3 the variance is expected and unquantified, and averaging them into
    #: one number would state a precision the design cannot support.
    comparability_policy: Literal["paired_within_repetition_only"]
    #: What the effective configuration actually shows. `None` means the key is
    #: absent, which is a finding. Writing 0.0 for an unset temperature would
    #: translate an absence into a setting nobody made.
    temperature: float | None = None
    temperature_state: Literal["unset_provider_default", "explicit"]
    sampling_params_observed: dict[str, int | float | str]

    @model_validator(mode="after")
    def validate_no_invented_determinism(self) -> SamplingAudit:
        if self.seed_reaches_provider_request:
            if not self.provider_request_field:
                raise ValueError(
                    "a seed said to reach the request must name the field"
                )
            if self.model_sampling != "seeded":
                raise ValueError("a propagated seed means sampling is seeded")
        else:
            if self.provider_request_field:
                raise ValueError(
                    "no seed reaches the request, so naming a field for it "
                    "would describe a path that does not exist"
                )
            if self.model_sampling != "stochastic":
                raise ValueError(
                    "no seed reaches the request, so sampling is stochastic; "
                    "recording anything else invents determinism"
                )
        if self.temperature_state == "unset_provider_default":
            if self.temperature is not None:
                raise ValueError(
                    "temperature is recorded as unset, so it must be null. "
                    "Writing a number here turns an absence into a setting."
                )
        elif self.temperature is None:
            raise ValueError("an explicit temperature must carry its value")
        return self


class RepetitionIdentity(StrictModel):
    """One repetition. Not a seed, and named so.

    `repetition_identity` replaces the earlier `sampling_seed`. The old term
    survives only in superseded historical records, which are preserved with a
    supersession note rather than rewritten.
    """

    repetition_id: str = Field(min_length=1)
    repetition_identity: int = Field(ge=1)
    session_identity: str = Field(min_length=1)


class ArmExecution(StrictModel):
    """One arm of one repetition, and where it sits in the schedule."""

    arm: ArmKind
    order_within_repetition: int = Field(ge=1, le=2)
    fresh_clone_of_seed_commit: str = Field(pattern=_GIT40)
    qwen_home: str = Field(min_length=1)
    worktree: str = Field(min_length=1)
    evidence_directory: str = Field(min_length=1)


class PairedExecution(StrictModel):
    """One repetition: two arms, ordered, with everything else identical."""

    repetition: RepetitionIdentity
    executions: tuple[ArmExecution, ...] = Field(min_length=2, max_length=2)

    @model_validator(mode="after")
    def validate_pair_is_balanced_and_isolated(self) -> PairedExecution:
        arms = {item.arm for item in self.executions}
        if arms != {ArmKind.DEFAULT_QWEN_CONTROL, ArmKind.APOAPSIS_SANDBOX}:
            raise ValueError("a pair is exactly one control and one sandbox arm")
        orders = sorted(item.order_within_repetition for item in self.executions)
        if orders != [1, 2]:
            raise ValueError("the two arms must be ordered 1 then 2")
        commits = {item.fresh_clone_of_seed_commit for item in self.executions}
        if len(commits) != 1:
            raise ValueError("both arms must clone the same seed commit")
        for field in ("qwen_home", "worktree", "evidence_directory"):
            values = {getattr(item, field) for item in self.executions}
            if len(values) != len(self.executions):
                raise ValueError(
                    f"both arms share a {field}; the second arm would inherit "
                    "the first's state and the pair would be contaminated"
                )
        return self


class MountVisibility(StrEnum):
    BOTH_ARMS = "both_arms"
    CONTROLLER_ONLY = "controller_only"


class MountSpec(StrictModel):
    """One mount, with who can see it.

    `EVALUATOR_ONLY` package artifacts have no `MountVisibility` that admits
    them, which is the point: there is no value of this field that puts the
    oracle or either candidate inside an arm.
    """

    source_identity: str = Field(min_length=1)
    destination: str = Field(min_length=1)
    mode: Literal["ro", "rw"]
    purpose: str = Field(min_length=1)
    visibility: MountVisibility


class NetworkAndMountPolicy(StrictModel):
    """Containment, frozen identically for both arms."""

    network: Literal["none"] = "none"
    egress: Literal["controller_owned_unix_socket_relay"] = (
        "controller_owned_unix_socket_relay"
    )
    direct_upstream_route: Literal[False] = False
    hardened_workcell: Literal[True] = True
    separate_worktrees: Literal[True] = True
    separate_qwen_homes: Literal[True] = True
    durable_evaluator_side_evidence: Literal[True] = True
    mounts: tuple[MountSpec, ...] = Field(min_length=1)
    #: Supervisor internals differ between arms by construction -- that is what
    #: is being compared -- so they are declared, and declared not to carry
    #: solution information.
    arm_specific_supervisor_internals: dict[str, str]
    supervisor_internals_carry_no_solution_information: Literal[True] = True

    def arm_visible_mounts(self) -> tuple[MountSpec, ...]:
        return tuple(
            item for item in self.mounts if item.visibility is MountVisibility.BOTH_ARMS
        )

    def policy_digest(self) -> str:
        return canonical_digest(self.model_dump(mode="json"))


class RepairPolicy(StrictModel):
    """Where the scored boundary is, and what may cross it.

    The Crisis Atlas record exists because a proposal was scored by what it
    eventually became rather than by what it was. So the first proposal is
    scored before any repair, and no repair can improve that score. Repairs
    are not banned -- they are recorded as separate authoritative checkpoints,
    with delivered and repaired quality kept apart from proposal quality.
    """

    first_proposal_scored_before_repair: Literal[True] = True
    repair_may_improve_proposal_score: Literal[False] = False
    qwen_native_continuation_allowed: bool
    qwen_native_continuation_budget: int = Field(ge=0)
    frontier_repair_allowed_during_scored_phase: Literal[False] = False
    human_repair_allowed_during_scored_phase: Literal[False] = False
    post_score_repair_allowed: bool
    post_score_repair_recorded_as_separate_checkpoint: Literal[True] = True
    proposal_quality_separate_from_delivered_quality: Literal[True] = True
    #: Every route by which a repair could occur must be named. An unnamed
    #: route is an unrecorded one.
    enumerated_repair_routes: tuple[str, ...] = Field(min_length=1)
    unrecorded_repair_path_permitted: Literal[False] = False


class Budgets(StrictModel):
    """Spend ceilings, equal across arms for the scored phase."""

    context_limit_tokens: int = Field(ge=1)
    max_output_tokens: int = Field(ge=1)
    per_provider_call_output_cap: int = Field(ge=1)
    per_arm_wall_clock_seconds: float = Field(gt=0)
    total_token_budget: int = Field(ge=1)
    continuation_limit: int = Field(ge=0)
    no_progress_limit: int = Field(ge=0)
    emergency_call_policy: str = Field(min_length=1)
    verification_timeout_seconds: float = Field(gt=0)
    launch_timeout_seconds: float = Field(gt=0)
    repair_budget_calls: int = Field(ge=0)
    equivalent_model_spend_opportunity: Literal[True] = True
    #: Harness verification cost is real and is recorded, but separately. Rolled
    #: into model cost it would make the sandbox look more expensive for work
    #: the control never had to do.
    harness_verification_cost_recorded_separately: Literal[True] = True

    @model_validator(mode="after")
    def validate_output_fits_context(self) -> Budgets:
        if self.max_output_tokens > self.context_limit_tokens:
            raise ValueError(
                "max_output_tokens exceeds the context window; a run pinned "
                "this way reports output truncation for context exhaustion"
            )
        return self


class HashedLibrary(StrictModel):
    absolute_path: str = Field(min_length=1)
    size_bytes: int = Field(gt=0)
    sha256: str = Field(pattern=_SHA256)


class ServerDependencyClosure(StrictModel):
    """What actually performs inference, as opposed to what launches it.

    `llama-server` is 17,920 bytes. Hashing it identifies a launcher and
    almost nothing about the arithmetic: the implementation lives in
    `libllama-server-impl.so` (7.2MB) and `libggml-cuda.so` (63.4MB), either of
    which can be swapped without the launcher's digest moving at all.

    So llama/ggml-owned libraries are hashed individually. System libraries are
    identified by package version instead -- hashing glibc would bind the run
    to a patch level nobody controls and would change on an unrelated security
    update, which is noise rather than identity. CUDA and the GPU driver are
    recorded by version for the same reason, and because the driver is supplied
    by the Windows host through `/usr/lib/wsl/lib` and is not a file this
    repository can pin.

    This closure is a *static* claim. Live preflight must recheck it, because
    `LD_LIBRARY_PATH` at run time is what decides which of these actually load.
    """

    launcher: HashedLibrary
    #: llama.cpp and ggml artifacts: hashed, because we own their identity.
    hashed_libraries: tuple[HashedLibrary, ...] = Field(min_length=4)
    #: System libraries: package versions, because we do not.
    system_libraries: dict[str, str]
    cuda_sdk_version: str = Field(min_length=1)
    cuda_runtime_versions: dict[str, str]
    gpu_name: str = Field(min_length=1)
    gpu_driver_version: str = Field(min_length=1)
    gpu_memory_total_mib: int = Field(gt=0)
    wsl_distribution: str = Field(min_length=1)
    wsl_kernel: str = Field(min_length=1)
    architecture: str = Field(min_length=1)
    recheck_at_live_preflight: Literal[True] = True

    @model_validator(mode="after")
    def validate_implementation_is_present(self) -> ServerDependencyClosure:
        names = {item.absolute_path.rsplit("/", 1)[-1] for item in self.hashed_libraries}
        required = {"libllama-server-impl.so"}
        missing = required - names
        if missing:
            raise ValueError(
                f"the closure omits {sorted(missing)}, which is where the "
                "server's request handling actually lives; without it the "
                "launcher digest identifies almost nothing"
            )
        if self.launcher.size_bytes > 1_000_000:
            raise ValueError(
                "the launcher is unexpectedly large; if it is now statically "
                "linked this closure model no longer describes the build"
            )
        return self


class ImageProvenance(StrictModel):
    """An image plus the commit it is provably built from.

    An image id proves that bytes exist, not where they came from.
    `apoapsis-live-controller:slice5c` was built `FROM ...:slice2c` with a
    `COPY src` from a working tree and carries no labels at all, so no commit
    can be attributed to it. `provenance_proven` records honestly which
    situation an image is in, and the pilot controller is rebuilt so it can be
    true.
    """

    image: str = Field(min_length=1)
    image_id: str = Field(pattern=_IMAGE_DIGEST)
    provenance_proven: bool
    source_commit: str | None = Field(default=None, pattern=_GIT40)
    source_tree: str | None = Field(default=None, pattern=_GIT40)
    build_context_sha256: str | None = Field(default=None, pattern=_SHA256)
    dockerfile_path: str | None = None
    dockerfile_sha256: str | None = Field(default=None, pattern=_SHA256)
    build_command: str | None = None
    #: Labels read back off the built image, so the claim is checkable against
    #: the artefact rather than against the build log.
    labels: dict[str, str] = Field(default_factory=dict)
    note: str | None = None

    @model_validator(mode="after")
    def validate_proven_images_carry_their_source(self) -> ImageProvenance:
        if self.provenance_proven:
            missing = [
                name
                for name in (
                    "source_commit",
                    "source_tree",
                    "build_context_sha256",
                    "dockerfile_sha256",
                    "build_command",
                )
                if getattr(self, name) is None
            ]
            if missing:
                raise ValueError(
                    f"provenance_proven is true but {missing} are unrecorded"
                )
            label_commit = self.labels.get("org.apoapsis.source-commit")
            if label_commit != self.source_commit:
                raise ValueError(
                    "the image's own label disagrees with the recorded source "
                    f"commit ({label_commit} vs {self.source_commit}). A "
                    "cached LABEL layer can retain another build's args."
                )
            label_context = self.labels.get("org.apoapsis.build-context-sha256")
            if label_context != self.build_context_sha256:
                raise ValueError(
                    "the image's build-context label disagrees with the "
                    "recorded context digest; this is the cached-layer defect"
                )
        elif not self.note:
            raise ValueError(
                "an image without proven provenance must say why, so the gap "
                "is visible rather than merely absent"
            )
        return self


class CrisisAtlasBinding(StrictModel):
    """The case package, bound by digest at the point of freezing."""

    package_root: str = Field(min_length=1)
    package_digest: str = Field(pattern=_SHA256)
    package_registerable: Literal[True] = True
    qualification_evidence_root: str = Field(min_length=1)
    qualification_evidence_sha256: str = Field(pattern=_SHA256)
    seed_commit: str = Field(pattern=_GIT40)
    seed_tree: str = Field(pattern=_GIT40)
    task_sha256: str = Field(pattern=_SHA256)
    plan_contract_sha256: str = Field(pattern=_SHA256)
    acceptance_criteria_sha256: str = Field(pattern=_SHA256)
    verification_commands_sha256: str = Field(pattern=_SHA256)
    expected_witness_sha256: str = Field(pattern=_SHA256)
    authoritative_checkpoint_policy: str = Field(min_length=1)
    evaluator_only_assets_remain_evaluator_only: Literal[True] = True


class StopCondition(StrEnum):
    """Conditions that block inference or make a pair `INCOMPARABLE`.

    None of them is ever convertible into a pass. That is the entire reason
    this is an enumeration in the frozen manifest rather than a checklist
    someone consults: a condition that can be waived at run time is not a stop
    condition, it is a preference.
    """

    MODEL_SERVER_OR_QWEN_IDENTITY_DIFFERS = "model_server_or_qwen_identity_differs"
    CODING_PROFILE_OR_REALISED_TOOLS_DIFFER = "coding_profile_or_realised_tools_differ"
    CONTAINMENT_OR_RELAY_READINESS_FAILS = "containment_or_relay_readiness_fails"
    PROVIDER_PROTOCOL_CONFORMANCE_FAILS = "provider_protocol_conformance_fails"
    PACKAGE_NO_LONGER_REGISTERABLE = "package_no_longer_registerable"
    MANIFEST_OR_LOCK_DIGEST_DIFFERS = "manifest_or_lock_digest_differs"
    SEED_TASK_OR_CONTRACT_BYTES_DIFFER = "seed_task_or_contract_bytes_differ"
    TASK_VISIBLE_INFORMATION_DIFFERS_BETWEEN_ARMS = (
        "task_visible_information_differs_between_arms"
    )
    REQUIRED_ARTIFACT_UNRESOLVED = "required_artifact_unresolved"
    EVIDENCE_STORAGE_NOT_DURABLE = "evidence_storage_not_durable"
    EVALUATOR_ONLY_MATERIAL_EXPOSED = "evaluator_only_material_exposed"
    COLD_WARM_STATE_DIFFERS = "cold_warm_state_differs"
    PRIOR_ARM_CONTAMINATION_DETECTED = "prior_arm_contamination_detected"
    TELEMETRY_CANNOT_BE_CLASSIFIED = "telemetry_cannot_be_classified"
    REPETITION_CONFIGURATION_DIFFERS = "repetition_configuration_differs"
    EXECUTION_IMAGE_NOT_BUILT_FROM_PINNED_SOURCE = (
        "execution_image_not_built_from_pinned_source"
    )
    ACCEPTANCE_OBLIGATIONS_UNMAPPED = "acceptance_obligations_unmapped"
    DEPENDENCY_CLOSURE_DIFFERS = "dependency_closure_differs"


class ColdWarmProtocol(StrictModel):
    """Specified now, executed later. None of it runs in 7P.2.

    Step 2 is a model call. It is written down here precisely so that it is
    obvious it has *not* happened: a readiness request is inference, and this
    phase issues none.
    """

    steps: tuple[str, ...] = Field(min_length=6)
    readiness_request_is_inference: Literal[True] = True
    readiness_request_executed_in_this_phase: Literal[False] = False
    cold_load_and_warm_timings_recorded_separately: Literal[True] = True
    incomparable_if_state_not_equivalent: Literal[True] = True


class PilotAuthority(StrictModel):
    """The executables that may decide a verdict, bound by committed bytes.

    Added in schema 2.0 because 7P.3 could not rehearse: schema 1.0 bound no
    runner at all, so any runner written afterwards would have been unbound
    code deciding an experimental outcome. `verify_authority` checks these
    against Git objects, never the working tree.
    """

    authority_commit: str = Field(pattern=_GIT40)
    bound_modules: tuple[dict[str, str], ...] = Field(min_length=4)
    fake_provider_script_sha256: str = Field(pattern=_SHA256)
    rationale: str = Field(min_length=1)


class Supersession(StrictModel):
    """What this manifest replaces, and why the replaced pair was invalid.

    The superseded artifacts are preserved rather than edited. Rewriting them
    to look correct would destroy the record of what was actually locked when,
    which is the only reason anyone can now say the old pair was never
    rehearsed and never authorised anything.
    """

    manifest_path: str = Field(min_length=1)
    manifest_digest: str = Field(pattern=_SHA256)
    manifest_commit: str = Field(min_length=7)
    lock_path: str = Field(min_length=1)
    lock_digest: str = Field(pattern=_SHA256)
    lock_commit: str = Field(min_length=7)
    status: Literal["superseded"] = "superseded"
    ever_rehearsed: Literal[False] = False
    ever_authorized_for_live_inference: Literal[False] = False
    invalid_because: tuple[str, ...] = Field(min_length=1)
    preserved_not_edited: Literal[True] = True


class PackageEvidenceReuse(StrictModel):
    """Whether the eight real proofs were reused, and on what evidence.

    Reuse is permitted only when the modules that produced the evidence are
    byte-identical between authorities. Comparing commits would be too strict
    and comparing behaviour too weak; comparing blobs is the actual question.
    """

    reused: bool
    reason: str = Field(min_length=1)
    changed_modules: tuple[str, ...] = ()
    requalified_at: str | None = Field(default=None, pattern=_GIT40)
    all_eight_proofs_passed: bool

    @model_validator(mode="after")
    def validate_requalification_is_recorded(self) -> PackageEvidenceReuse:
        if not self.reused and self.requalified_at is None:
            raise ValueError(
                "evidence was not reused, so the commit it was regenerated at "
                "must be recorded; otherwise the evidence names no authority"
            )
        if self.reused and self.changed_modules:
            raise ValueError(
                "evidence cannot be reused while its authority modules changed"
            )
        return self


class PilotManifest(StrictModel):
    """One case, three repetitions, two arms, frozen before any result exists.

    `ready_for_inference()` becoming true does not authorise a run. It says the
    manifest is complete. Authorisation to *rehearse* comes only from a lock,
    written in a later commit, and even that is not authorisation to call a
    model.
    """

    #: 2.0 adds `pilot_authority`, `supersedes` and `package_evidence_reuse`.
    #: 1.0 remains readable so the superseded manifest can still be loaded and
    #: compared rather than only looked at.
    schema_version: Literal["1.0", "2.0"] = PILOT_MANIFEST_SCHEMA_VERSION
    manifest_id: str = Field(min_length=1)
    created_utc: str = Field(min_length=1)
    scope: PilotScope
    subject_implementation_commit: str = Field(pattern=_GIT40)
    evaluator_framework_commit: str = Field(pattern=_GIT40)
    model: ModelIdentity
    server: ServerIdentity
    server_dependency_closure: ServerDependencyClosure
    threshold_ladder: ThresholdLadder
    qwen: QwenIdentity
    controller_image: ImageProvenance
    workcell_image: ImageProvenance
    sampling: SamplingAudit
    crisis_atlas: CrisisAtlasBinding
    network_and_mounts: NetworkAndMountPolicy
    repair: RepairPolicy
    budgets: Budgets
    paired_executions: tuple[PairedExecution, ...] = Field(min_length=3, max_length=3)
    cold_warm: ColdWarmProtocol
    stop_conditions: tuple[StopCondition, ...] = Field(min_length=18)
    #: A single number combining proposal and detection would let one hide the
    #: other, which is how "COMPLETE with four green commands" happened.
    combined_score_defined: Literal[False] = False
    live_execution_authorised_by_manifest: Literal[False] = False
    #: Schema 2.0. Optional so a 1.0 manifest still parses; required in
    #: practice by `unresolved_hashes`, which reports a 2.0 manifest without
    #: an authority as unresolved rather than accepting it.
    pilot_authority: PilotAuthority | None = None
    supersedes: Supersession | None = None
    package_evidence_reuse: PackageEvidenceReuse | None = None

    @model_validator(mode="after")
    def validate_pilot_is_frozen_and_honest(self) -> PilotManifest:
        identities = [item.repetition.repetition_id for item in self.paired_executions]
        if len(set(identities)) != REQUIRED_REPETITIONS:
            raise ValueError(
                f"three distinct repetitions are required; got {identities}"
            )
        declared = set(self.stop_conditions)
        if declared != set(StopCondition):
            missing = sorted(str(item) for item in set(StopCondition) - declared)
            raise ValueError(
                f"the manifest omits stop conditions {missing}; an omitted "
                "condition is one nothing will refuse on"
            )
        if self.budgets.context_limit_tokens != self.threshold_ladder.context_window:
            raise ValueError(
                "the budget's context limit and the measured ladder describe "
                "different windows, so the ladder does not apply to this run"
            )
        for spec in self.network_and_mounts.mounts:
            if spec.visibility is MountVisibility.BOTH_ARMS and (
                "evaluator-only" in spec.source_identity
            ):
                raise ValueError(
                    f"mount {spec.source_identity!r} is evaluator-only and "
                    "visible to both arms"
                )
        return self

    def unresolved_hashes(self) -> tuple[str, ...]:
        """Controlled variables still carrying no measured identity."""

        unresolved: list[str] = []
        if not self.controller_image.provenance_proven:
            unresolved.append("controller_image.source_commit")
        if self.model.digest_source != "recomputed_from_file":
            unresolved.append("model.sha256")
        if self.schema_version == "2.0" and self.pilot_authority is None:
            # A 2.0 manifest without an authority is the 7P.3 state: complete
            # data and no bound executable to act on it.
            unresolved.append("pilot_authority")
        return tuple(unresolved)

    def ready_for_inference(self) -> bool:
        """Every controlled variable resolves. Not an authorisation."""

        return not self.unresolved_hashes()

    def digest(self) -> str:
        return canonical_digest(self.model_dump(mode="json"))


class PilotLock(StrictModel):
    """Binds a finalized manifest. Authorises the rehearsal, nothing more.

    Written in a *separate* commit from the manifest so it can name the
    manifest's commit hash without containing it -- a lock that embedded the
    hash of the commit it lives in could never be written truthfully.

    `authorises_live_inference` is `Literal[False]` and there is no field that
    can turn it true. Authorisation to call a model is a later decision with
    its own gates; the lock's job is to make the zero-token rehearsal possible
    and to make any drift afterwards detectable.
    """

    schema_version: Literal["1.0"] = PILOT_LOCK_SCHEMA_VERSION
    lock_id: str = Field(min_length=1)
    created_utc: str = Field(min_length=1)
    manifest_path: str = Field(min_length=1)
    manifest_digest: str = Field(pattern=_SHA256)
    manifest_commit: str = Field(pattern=_GIT40)
    subject_implementation_commit: str = Field(pattern=_GIT40)
    evaluator_framework_commit: str = Field(pattern=_GIT40)
    crisis_atlas_package_digest: str = Field(pattern=_SHA256)
    qualification_evidence_sha256: str = Field(pattern=_SHA256)
    paired_execution_identities: tuple[str, ...] = Field(min_length=3, max_length=3)
    authorises_zero_token_rehearsal: Literal[True] = True
    authorises_live_inference: Literal[False] = False

    @model_validator(mode="after")
    def validate_distinct_pairs(self) -> PilotLock:
        if len(set(self.paired_execution_identities)) != REQUIRED_REPETITIONS:
            raise ValueError("the three paired execution identities must differ")
        return self

    def digest(self) -> str:
        return canonical_digest(self.model_dump(mode="json"))

    def verify_against(self, manifest: PilotManifest) -> None:
        """Refuse a lock that does not describe this manifest."""

        if manifest.digest() != self.manifest_digest:
            raise ValueError(
                "the manifest has changed since it was locked: it now digests "
                f"to {manifest.digest()}, the lock binds {self.manifest_digest}"
            )
        if manifest.subject_implementation_commit != self.subject_implementation_commit:
            raise ValueError("subject implementation commit differs from the lock")
        if manifest.crisis_atlas.package_digest != self.crisis_atlas_package_digest:
            raise ValueError("the case package digest differs from the lock")
        expected = tuple(
            item.repetition.repetition_id for item in manifest.paired_executions
        )
        if expected != self.paired_execution_identities:
            raise ValueError("the paired execution identities differ from the lock")


class RehearsalAuthorization(StrictModel):
    """Whether the zero-token rehearsal may proceed, and why not if it may not."""

    authorized: bool
    reason: str = Field(min_length=1)


def authorize_rehearsal(
    manifest: PilotManifest, lock: PilotLock | None
) -> RehearsalAuthorization:
    """No lock, no rehearsal. A complete manifest is not permission."""

    if lock is None:
        return RehearsalAuthorization(
            authorized=False,
            reason=(
                "no lock exists. A manifest whose every variable resolves is "
                "complete, not authorised; the lock is the separate act that "
                "authorises the zero-token rehearsal."
            ),
        )
    if not manifest.ready_for_inference():
        return RehearsalAuthorization(
            authorized=False,
            reason=(
                "the manifest still has unresolved controlled variables: "
                f"{list(manifest.unresolved_hashes())}"
            ),
        )
    try:
        lock.verify_against(manifest)
    except ValueError as exc:
        return RehearsalAuthorization(authorized=False, reason=str(exc))
    return RehearsalAuthorization(
        authorized=True,
        reason=(
            "the lock binds this manifest exactly. This authorises the "
            "zero-token rehearsal only; live inference is not authorised."
        ),
    )


class ExecutionRecordRefused(RuntimeError):
    pass


def accept_execution_record(
    record: dict, *, manifest: PilotManifest, lock: PilotLock
) -> None:
    """Refuse a record that does not name this manifest and this lock.

    Execution records reference the frozen artifacts; they never edit them. A
    record naming another digest is describing a different experiment, and
    filing it here would silently merge two.
    """

    for field, expected in (
        ("manifest_digest", manifest.digest()),
        ("lock_digest", lock.digest()),
    ):
        actual = record.get(field)
        if actual is None:
            raise ExecutionRecordRefused(f"the record declares no {field}")
        if actual != expected:
            raise ExecutionRecordRefused(
                f"{field} is {actual}, but this pilot is {expected}. The "
                "record describes a different experiment."
            )
