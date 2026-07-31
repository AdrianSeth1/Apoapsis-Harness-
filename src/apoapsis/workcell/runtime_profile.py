"""One reproducible runtime profile. Not a tuning framework.

The handoff lists eleven server knobs worth benchmarking — GPU offload, thread
counts, batch sizes, Flash Attention, KV-cache precision, slot counts, prompt
cache, memory locking, speculative decoding. Each is a real lever and the list
is a research programme. Running it would answer a question nobody is currently
asking.

The question actually open is whether Apoapsis Qwen matches or beats unharnessed
Qwen per case, with fewer false completions and lower median input tokens. A
faster server changes none of those unless it changes one of them, and a sweep
run before the paired corpus exists would produce a profile optimised against a
benchmark that cannot yet detect a quality regression.

So this module does one thing: it names the configuration that has **already
been qualified live** and makes it reproducible. `QUALIFIED_PROFILE` is not a
recommendation derived from measurement here; it is the transcription of the
configuration that passed Slice 5C containment, readiness, native compaction and
continuation, plus the threshold ladder ADR 0082 measured from it.

`OptimisationVerdict` records the ones deliberately not benchmarked, with the
reason, so a later session finds a decision rather than an oversight.
"""

from __future__ import annotations

from enum import Enum

from pydantic import Field

from apoapsis.specification.schema import StrictModel


class OptimisationVerdict(str, Enum):
    """Why an optimisation was or was not benchmarked."""

    #: Plausibly moves per-case proposal quality, false completion, latency, or
    #: tokens. Worth a controlled arm.
    CANDIDATE = "candidate"
    #: Not benchmarked, on purpose. Records a decision, not an omission.
    REJECTED_WITHOUT_BENCHMARK = "rejected_without_benchmark"
    #: Benchmarked and rejected. Reserved; nothing holds it yet, because no
    #: sweep has run.
    BENCHMARKED_AND_REJECTED = "benchmarked_and_rejected"


class OptimisationDecision(StrictModel):
    """One knob, and what was decided about it."""

    name: str = Field(min_length=1)
    verdict: OptimisationVerdict
    rationale: str = Field(min_length=1)


class RuntimeProfile(StrictModel):
    """The pinned runtime configuration for a comparable run.

    Every field is a value that was live during Slice 5C qualification. Nothing
    here is aspirational, and nothing was chosen by this module.
    """

    profile_id: str = Field(min_length=1)
    #: The evaluation record whose run these values were taken from.
    qualified_by: str = Field(min_length=1)
    model_name: str = Field(min_length=1)
    quantization: str = Field(min_length=1)
    server_name: str = Field(min_length=1)
    server_version: str = Field(min_length=1)
    context_limit_tokens: int = Field(ge=1)
    max_output_tokens: int = Field(ge=1)
    temperature: float = Field(ge=0)
    sampling_seed: int
    cli_name: str = Field(min_length=1)
    cli_version: str = Field(min_length=1)
    #: Reasoning disabled, as both Crisis Atlas arms ran. Enabling it is a
    #: candidate arm, not a default change.
    reasoning_enabled: bool = False
    #: The measured ladder, never a percentage. See ADR 0082.
    auto_compact_trigger_tokens: int = Field(ge=1)
    warn_tokens: int = Field(ge=0)
    hard_tokens: int = Field(ge=1)
    #: What was deliberately left alone.
    optimisation_decisions: list[OptimisationDecision] = Field(default_factory=list)


#: The Slice 5C configuration, transcribed. Changing any value here makes a run
#: incomparable to the qualified one, which is why they are constants rather
#: than defaults somebody can drift.
QUALIFIED_PROFILE = RuntimeProfile(
    profile_id="qwen3.6-27b-q4km-65k-2026-07-30",
    qualified_by="docs/evaluation/slice-5c-live-qualification-2026-07-30.md",
    model_name="qwen3.6-27b",
    quantization="Q4_K - Medium",
    server_name="llama-server",
    server_version="b10107-c0bc8591e",
    context_limit_tokens=65_536,
    max_output_tokens=16_384,
    temperature=0.0,
    sampling_seed=0,
    cli_name="@qwen-code/qwen-code",
    cli_version="0.21.1",
    reasoning_enabled=False,
    auto_compact_trigger_tokens=32_536,
    warn_tokens=12_536,
    hard_tokens=42_536,
    optimisation_decisions=[
        OptimisationDecision(
            name="llama-server tuning sweep",
            verdict=OptimisationVerdict.REJECTED_WITHOUT_BENCHMARK,
            rationale=(
                "GPU offload, thread counts, batch sizes, Flash Attention, "
                "KV-cache precision, slot counts and memory locking change "
                "throughput, not what the agent proposes. A sweep before the "
                "paired corpus exists optimises against a benchmark that "
                "cannot yet detect a quality regression."
            ),
        ),
        OptimisationDecision(
            name="speculative decoding",
            verdict=OptimisationVerdict.REJECTED_WITHOUT_BENCHMARK,
            rationale=(
                "Latency only, and it perturbs sampling. The handoff already "
                "forbids changing a default from latency alone."
            ),
        ),
        OptimisationDecision(
            name="KV-cache quantisation",
            verdict=OptimisationVerdict.REJECTED_WITHOUT_BENCHMARK,
            rationale=(
                "Memory headroom is not currently the binding constraint, and "
                "it risks long-context recall -- which is the one thing Slice "
                "5C actually established works."
            ),
        ),
        OptimisationDecision(
            name="compaction threshold tuning",
            verdict=OptimisationVerdict.REJECTED_WITHOUT_BENCHMARK,
            rationale=(
                "Slice 5 is frozen. The ladder is measured and delegated to "
                "the CLI; a second Apoapsis model of when compaction happens "
                "is the failure ADR 0082 was written about."
            ),
        ),
        OptimisationDecision(
            name="read-only tool parallelism",
            verdict=OptimisationVerdict.REJECTED_WITHOUT_BENCHMARK,
            rationale=(
                "Wall-clock only. Qwen Code warns forked agents share a "
                "worktree, and a concurrent write would make the fingerprint "
                "ambiguous -- which is the basis of both progress detection "
                "and witness binding."
            ),
        ),
        OptimisationDecision(
            name="reasoning-effort routing",
            verdict=OptimisationVerdict.CANDIDATE,
            rationale=(
                "The only knob on this list that plausibly moves per-case "
                "proposal quality and false completion rather than throughput. "
                "Kept as a candidate arm for the paired corpus; not enabled by "
                "default and not benchmarked here."
            ),
        ),
        OptimisationDecision(
            name="LSP diagnostics beyond syntax",
            verdict=OptimisationVerdict.CANDIDATE,
            rationale=(
                "Could reduce the Slice 3-style route and type mistakes that "
                "drive repair distance. Behind the same DiagnosticReport "
                "contract and the same NOT_CHECKED discipline when it lands."
            ),
        ),
    ],
)


def candidates(profile: RuntimeProfile = QUALIFIED_PROFILE) -> list[str]:
    """The optimisations worth an arm when the paired corpus exists."""

    return [
        item.name
        for item in profile.optimisation_decisions
        if item.verdict is OptimisationVerdict.CANDIDATE
    ]
