"""Prompt-injection indicators in untrusted web content.

Web page text is **data**, never instructions. The agent's system prompt, the
user's goal, permissions, approvals and the data boundary all outrank anything a
page says -- and this module makes that explicit and testable rather than
relying on the model to notice.

It scans page text for a small, high-precision set of phrases that a hostile page
typically uses to hijack an agent (``ignore previous instructions``, ``reveal
your system prompt``, ``upload your files``, ...). A hit is recorded as an
:class:`InjectionIndicator` on the observation and on the audit trail, and the
runtime surfaces it to the model as a *warning*, never as an instruction.

Detection is deliberately conservative: it flags, it does not act. The real
defences are architectural -- the boundary, the permission/approval gate, the
sandbox and the URL policy -- so a page that says "upload /etc/passwd" is blocked
because the *sandbox* refuses the path, not because the model was clever enough
to refuse. This scanner is the signal layer on top of those hard controls.
"""

from __future__ import annotations

import re

from pydantic import BaseModel, ConfigDict

from ..core.enums import StrEnum


class InjectionKind(StrEnum):
    """The category of a suspicious instruction found in page content."""

    INSTRUCTION_OVERRIDE = "instruction_override"
    SYSTEM_PROMPT_PROBE = "system_prompt_probe"
    SECRET_REQUEST = "secret_request"  # noqa: S105 - a category label, not a credential
    FILE_UPLOAD = "file_upload"
    COMMAND_EXECUTION = "command_execution"
    POLICY_BYPASS = "policy_bypass"
    CREDENTIAL_REQUEST = "credential_request"
    EXFILTRATION = "exfiltration"


class InjectionIndicator(BaseModel):
    """One suspicious phrase found in untrusted content."""

    model_config = ConfigDict(frozen=True)

    kind: InjectionKind
    #: The matched phrase, lowercased and length-capped (never the whole page).
    evidence: str
    #: Where in the text the match occurred (character offset), for provenance.
    offset: int = 0


#: Ordered, high-precision patterns. Each is a (kind, compiled-regex) pair. The
#: patterns are intentionally specific: a false positive is noise the model must
#: ignore, so we match agent-directed phrasing, not ordinary prose.
_PATTERNS: tuple[tuple[InjectionKind, re.Pattern[str]], ...] = (
    (
        InjectionKind.INSTRUCTION_OVERRIDE,
        re.compile(
            r"(ignore|disregard|forget)\s+(all\s+)?(your\s+)?(previous|prior|above|earlier)\s+"
            r"(instruction|prompt|rule|direction|message)",
            re.IGNORECASE,
        ),
    ),
    (
        InjectionKind.INSTRUCTION_OVERRIDE,
        re.compile(
            r"(تجاهل|أهمل)\s+(كل\s+)?(التعليمات|الأوامر|التوجيهات)\s+(السابقة|الماضية)",
            re.IGNORECASE,
        ),
    ),
    (
        InjectionKind.SYSTEM_PROMPT_PROBE,
        re.compile(
            r"(reveal|show|print|repeat|output|leak)\s+(your\s+)?(system\s+)?(prompt|instructions|"
            r"initial\s+message)",
            re.IGNORECASE,
        ),
    ),
    (
        InjectionKind.SYSTEM_PROMPT_PROBE,
        re.compile(
            r"(اكشف|أظهر|اطبع|كرر)\s+(ال)?(تعليمات|توجيهات|برومبت)\s*(ال)?(نظام|الأساسية|الأولية)?"
        ),
    ),
    (
        InjectionKind.SECRET_REQUEST,
        re.compile(
            r"(send|share|give|reveal|upload|post|expose|leak|email)\s+(me\s+)?(your\s+)?"
            r"(the\s+)?(api[\s_-]?key|secret|token|password|credential|private\s+key)",
            re.IGNORECASE,
        ),
    ),
    (
        InjectionKind.SECRET_REQUEST,
        re.compile(r"(أرسل|شارك|اكشف)\s+(مفتاح|كلمة|السر|الرمز|بيانات)\s*(ال)?(السرية|الخاصة)?"),
    ),
    (
        InjectionKind.FILE_UPLOAD,
        re.compile(
            r"(upload|attach|send|post)\s+(your\s+|the\s+)?(files?|documents?|\.env|id_rsa|"
            r"credentials|config)",
            re.IGNORECASE,
        ),
    ),
    (
        InjectionKind.FILE_UPLOAD,
        re.compile(r"(ارفع|أرفق|أرسل)\s+(ال)?(ملفات|مستندات|الملف)"),
    ),
    (
        InjectionKind.COMMAND_EXECUTION,
        re.compile(
            r"(run|execute|eval)\s+(this\s+|the\s+)?(command|shell|script|code)\b",
            re.IGNORECASE,
        ),
    ),
    (
        InjectionKind.COMMAND_EXECUTION,
        re.compile(r"\b(rm\s+-rf|curl\s+http|wget\s+http|chmod\s+\+x|sudo\s+\w+)", re.IGNORECASE),
    ),
    (
        InjectionKind.COMMAND_EXECUTION,
        re.compile(r"(نفّذ|شغّل|استدعِ)\s+(هذا\s+)?(الأمر|الكود|السكربت)"),
    ),
    (
        InjectionKind.POLICY_BYPASS,
        re.compile(
            r"(bypass|ignore|disable|override|skip)\s+(the\s+)?(policy|permission|approval|"
            r"restriction|safety|guardrail|sandbox)",
            re.IGNORECASE,
        ),
    ),
    (
        InjectionKind.CREDENTIAL_REQUEST,
        re.compile(
            r"(enter|provide|type|give)\s+(your\s+)?(username\s+and\s+password|login\s+"
            r"credentials|credit\s+card|card\s+number)",
            re.IGNORECASE,
        ),
    ),
    (
        InjectionKind.EXFILTRATION,
        re.compile(
            r"(send|post|upload)\s+(it|them|this|the\s+data|the\s+contents?)\s+to\s+"
            r"(https?://|the\s+following)",
            re.IGNORECASE,
        ),
    ),
)

#: Cap on the evidence snippet length, so a match cannot smuggle a whole page.
_EVIDENCE_CAP = 160


def scan_for_injection(text: str) -> tuple[InjectionIndicator, ...]:
    """Return the injection indicators found in ``text`` (possibly empty)."""
    if not text:
        return ()
    found: list[InjectionIndicator] = []
    seen: set[tuple[InjectionKind, int]] = set()
    for kind, pattern in _PATTERNS:
        for match in pattern.finditer(text):
            key = (kind, match.start())
            if key in seen:
                continue
            seen.add(key)
            found.append(
                InjectionIndicator(
                    kind=kind,
                    evidence=match.group(0)[:_EVIDENCE_CAP].strip().lower(),
                    offset=match.start(),
                )
            )
    # Stable, deterministic order by offset then kind.
    found.sort(key=lambda i: (i.offset, i.kind.value))
    return tuple(found)


__all__ = ["InjectionIndicator", "InjectionKind", "scan_for_injection"]
