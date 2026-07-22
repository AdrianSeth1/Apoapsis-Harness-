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
        distinct_sources = {item.source.value for item in unique.values()}
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
            selected.append(candidate)
            per_repository[repository] += 1
            per_source[candidate.source.value] += 1
            if len(selected) >= limit:
                break
        return selected, duplicate_count
