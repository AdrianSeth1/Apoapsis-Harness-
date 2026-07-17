from __future__ import annotations

from sol.research.schemas import LicenseClassification, ResearchSourceName


class LicenseClassifier:
    classifier_version = "1"

    _permissive = {
        "MIT",
        "APACHE-2.0",
        "BSD-2-CLAUSE",
        "BSD-3-CLAUSE",
        "ISC",
        "0BSD",
        "UNLICENSE",
    }
    _attribution = {"CC-BY-4.0", "CC-BY-3.0"}
    _incompatible = {
        "AGPL-3.0",
        "AGPL-3.0-ONLY",
        "AGPL-3.0-OR-LATER",
        "GPL-3.0",
        "GPL-3.0-ONLY",
        "GPL-3.0-OR-LATER",
        "GPL-2.0",
        "GPL-2.0-ONLY",
        "GPL-2.0-OR-LATER",
    }

    def classify(
        self,
        identifier: str | None,
        *,
        source: ResearchSourceName,
    ) -> LicenseClassification:
        if source == ResearchSourceName.REDDIT:
            return LicenseClassification.IDEA_ONLY
        if not identifier or identifier.upper() in {"NOASSERTION", "OTHER", "NONE"}:
            return LicenseClassification.IDEA_ONLY
        normalized = identifier.upper()
        if normalized in self._permissive:
            return LicenseClassification.CODE_REUSE_ALLOWED
        if normalized in self._attribution:
            return LicenseClassification.ATTRIBUTION_REQUIRED
        if normalized in self._incompatible:
            return LicenseClassification.LICENSE_INCOMPATIBLE
        if normalized.startswith(("LGPL", "MPL", "EPL", "CDDL")):
            return LicenseClassification.LICENSE_REVIEW_REQUIRED
        return LicenseClassification.LICENSE_REVIEW_REQUIRED
