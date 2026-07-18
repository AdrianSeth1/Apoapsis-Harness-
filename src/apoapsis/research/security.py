from __future__ import annotations

import re
from urllib.parse import urlparse

from apoapsis.research.schemas import PromptInjectionFlag


class ResearchSecurityError(RuntimeError):
    """External content or a URL violated deterministic research policy."""


class PromptInjectionDetector:
    detector_version = "1"

    _rules: tuple[tuple[str, re.Pattern[str], str], ...] = (
        (
            "ignore_instructions",
            re.compile(
                r"ignore (?:all |any )?(?:prior|previous) instructions", re.I
            ),
            "critical",
        ),
        (
            "reveal_prompt",
            re.compile(r"reveal (?:the )?(?:system|developer) prompt", re.I),
            "critical",
        ),
        (
            "run_command",
            re.compile(
                r"(?:run|execute) (?:this |the following )?(?:shell )?command",
                re.I,
            ),
            "high",
        ),
        (
            "download_execute",
            re.compile(r"download (?:it|this|and) (?:and )?execute", re.I),
            "critical",
        ),
        ("send_repository", re.compile(r"(?:send|upload) (?:the )?repository", re.I), "critical"),
        (
            "read_environment",
            re.compile(
                r"read (?:the )?(?:user'?s )?"
                r"(?:environment variables|\.env file)",
                re.I,
            ),
            "critical",
        ),
        (
            "disable_safety",
            re.compile(
                r"disable (?:the )?(?:safety|verification|security) checks",
                re.I,
            ),
            "critical",
        ),
        ("modify_rules", re.compile(r"modify (?:your|the) rules", re.I), "high"),
        ("use_token", re.compile(r"use this (?:access |api )?token", re.I), "critical"),
        ("upload_file", re.compile(r"upload this file", re.I), "high"),
        ("mark_trusted", re.compile(r"mark this source as trusted", re.I), "high"),
        ("curl_command", re.compile(r"\bcurl\b.*(?:\||&&|;)", re.I), "high"),
    )

    def detect(self, content: str) -> list[PromptInjectionFlag]:
        flags: list[PromptInjectionFlag] = []
        for line_number, line in enumerate(content.splitlines(), start=1):
            for rule_id, pattern, severity in self._rules:
                match = pattern.search(line)
                if match:
                    flags.append(
                        PromptInjectionFlag(
                            rule_id=rule_id,
                            phrase=match.group(0)[:200],
                            line_number=line_number,
                            severity=severity,
                        )
                    )
        return flags

    def sanitize(
        self, content: str
    ) -> tuple[str, list[PromptInjectionFlag]]:
        flags = self.detect(content)
        flagged_lines = {item.line_number for item in flags}
        sanitized = "\n".join(
            "[REMOVED POSSIBLE PROMPT INJECTION]"
            if line_number in flagged_lines
            else line
            for line_number, line in enumerate(content.splitlines(), start=1)
        )
        return sanitized, flags

    def contains_instruction(self, content: str) -> bool:
        return bool(self.detect(content))


def quarantine(content: str, source_id: str) -> str:
    return (
        f"UNTRUSTED_EXTERNAL_CONTENT_START source={source_id}\n"
        f"{content}\n"
        "UNTRUSTED_EXTERNAL_CONTENT_END"
    )


def validate_domain(url: str, allow_domains: list[str]) -> str:
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise ResearchSecurityError("research URLs must use HTTPS with a hostname")
    hostname = parsed.hostname.lower().rstrip(".")
    allowed = {
        domain.lower().rstrip(".") for domain in allow_domains if domain.strip()
    }
    if not any(hostname == domain or hostname.endswith(f".{domain}") for domain in allowed):
        raise ResearchSecurityError(f"research domain is not allowlisted: {hostname}")
    if parsed.username or parsed.password:
        raise ResearchSecurityError("research URLs must not contain credentials")
    return hostname
