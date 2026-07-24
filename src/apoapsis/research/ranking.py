from __future__ import annotations

from collections import defaultdict

from apoapsis.research.schemas import CandidateRanking, SourceCandidate


class SourceRanker:
    def rank(
        self,
        candidates: list[SourceCandidate],
        model_rankings: list[CandidateRanking],
        *,
        limit: int,
    ) -> tuple[list[SourceCandidate], int]:
        relevance = {item.candidate_id: item.relevance for item in model_rankings}
        unique: dict[str, SourceCandidate] = {}
        duplicate_count = 0
        for candidate in candidates:
            existing = unique.get(candidate.deduplication_key)
            if existing is None:
                unique[candidate.deduplication_key] = candidate
                continue
            duplicate_count += 1
            if candidate.deterministic_score > existing.deterministic_score:
                unique[candidate.deduplication_key] = candidate
        scored = sorted(
            unique.values(),
            key=lambda item: (
                -(
                    0.7 * item.deterministic_score
                    + 0.3 * relevance.get(item.candidate_id, 0.5)
                ),
                item.source.value,
                item.deduplication_key,
            ),
        )
        selected: list[SourceCandidate] = []
        per_repository: dict[str, int] = defaultdict(int)
        per_source: dict[str, int] = defaultdict(int)
        per_question: dict[str, int] = defaultdict(int)
        distinct_sources = {item.source.value for item in unique.values()}
        # Only cap per research question when more than one question is
        # actually represented among the candidates -- a single-question
        # research task (or fixtures/tests predating this field, which
        # leave it unset) must behave exactly as before this fairness rule
        # was added.
        distinct_questions = {
            item.research_question_id
            for item in unique.values()
            if item.research_question_id
        }
        for candidate in scored:
            repository = candidate.repository or candidate.deduplication_key
            if per_repository[repository] >= 2:
                continue
            source_limit = (
                limit
                if len(distinct_sources) == 1
                else max(2, (limit + 1) // 2)
            )
            if per_source[candidate.source.value] >= source_limit:
                continue
            if candidate.research_question_id and len(distinct_questions) > 1:
                # A broad query for one research question (classically: one
                # sweeping GitHub search) must not consume the entire fetch
                # allowance and starve every other question's candidates.
                question_limit = max(2, (limit + 1) // 2)
                if per_question[candidate.research_question_id] >= question_limit:
                    continue
            selected.append(candidate)
            per_repository[repository] += 1
            per_source[candidate.source.value] += 1
            if candidate.research_question_id:
                per_question[candidate.research_question_id] += 1
            if len(selected) >= limit:
                break
        return selected, duplicate_count
