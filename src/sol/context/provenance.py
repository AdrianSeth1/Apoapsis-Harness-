from __future__ import annotations

import hashlib
from enum import StrEnum

from pydantic import ConfigDict, Field, model_validator

from sol.specification.schema import StrictModel


class EvidenceKind(StrEnum):
    FILE_EXCERPT = "file_excerpt"
    SYMBOL = "symbol"
    TEST = "test"
    DIFF = "diff"
    FAILURE = "failure"
    GIT_HISTORY = "git_history"
    CONFIGURATION = "configuration"


class TransmissionPolicy(StrEnum):
    LOCAL_ONLY = "local_only"
    CLOUD_ALLOWED = "cloud_allowed"
    REDACT_BEFORE_CLOUD = "redact_before_cloud"
    APPROVED_PROVIDER_ONLY = "approved_provider_only"
    HUMAN_APPROVAL_REQUIRED = "human_approval_required"


class ContextEvidence(StrictModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)

    evidence_id: str = Field(pattern=r"^EV-[A-Za-z0-9._-]+$")
    kind: EvidenceKind
    path: str = Field(min_length=1)
    start_line: int | None = Field(default=None, ge=1)
    end_line: int | None = Field(default=None, ge=1)
    symbol: str | None = None
    commit: str = Field(min_length=1)
    reason_included: str = Field(min_length=1)
    content: str
    content_sha256: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )
    transmission_policy: TransmissionPolicy = TransmissionPolicy.LOCAL_ONLY

    @model_validator(mode="after")
    def validate_location_and_digest(self) -> ContextEvidence:
        if (self.start_line is None) != (self.end_line is None):
            raise ValueError("start_line and end_line must be provided together")
        if (
            self.start_line is not None
            and self.end_line is not None
            and self.end_line < self.start_line
        ):
            raise ValueError("end_line must not precede start_line")
        digest = hashlib.sha256(self.content.encode("utf-8")).hexdigest()
        if self.content_sha256 is None:
            self.content_sha256 = digest
        elif self.content_sha256 != digest:
            raise ValueError("content_sha256 does not match content")
        return self
