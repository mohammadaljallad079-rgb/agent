"""Message catalogs and locale helpers.

Keys are stable identifiers; the Arabic text is authored first (it is the
product's primary market), then English. ``translate`` never raises for a
missing key — it falls back to the key so a gap is visible, not silent.
"""

from __future__ import annotations

from ..core.enums import Direction, Locale, StrEnum


class MessageKey(StrEnum):
    RUN_STARTED = "run.started"
    RUN_COMPLETED = "run.completed"
    RUN_FAILED = "run.failed"
    APPROVAL_REQUIRED = "approval.required"
    APPROVAL_GRANTED = "approval.granted"
    APPROVAL_DENIED = "approval.denied"
    PERMISSION_DENIED = "permission.denied"
    TOOL_NOT_FOUND = "tool.not_found"
    SPECIALIST_UNKNOWN = "specialist.unknown"
    SPECIALIST_SCOPE_DENIED = "specialist.scope_denied"
    SPECIALIST_DEGRADED = "specialist.degraded"
    INVALID_REQUEST = "request.invalid"
    UNAUTHENTICATED = "auth.unauthenticated"
    NOT_FOUND = "resource.not_found"
    INTERNAL_ERROR = "error.internal"


#: Arabic is listed first deliberately: it is the primary product language.
MESSAGES: dict[Locale, dict[MessageKey, str]] = {
    Locale.AR: {
        MessageKey.RUN_STARTED: "بدأ تنفيذ المهمة.",
        MessageKey.RUN_COMPLETED: "تمت المهمة بنجاح.",
        MessageKey.RUN_FAILED: "فشلت المهمة.",
        MessageKey.APPROVAL_REQUIRED: "مطلوب موافقة المستخدم لإتمام العملية.",
        MessageKey.APPROVAL_GRANTED: "تمت الموافقة على العملية.",
        MessageKey.APPROVAL_DENIED: "تم رفض العملية.",
        MessageKey.PERMISSION_DENIED: "غير مسموح بتنفيذ هذه العملية.",
        MessageKey.TOOL_NOT_FOUND: "الأداة المطلوبة غير متوفرة.",
        MessageKey.SPECIALIST_UNKNOWN: "التخصص المطلوب غير معروف.",
        MessageKey.SPECIALIST_SCOPE_DENIED: "هذه الأداة خارج نطاق تخصص الوكيل الحالي.",
        MessageKey.SPECIALIST_DEGRADED: "بعض أدوات هذا التخصص غير متوفرة في بيئة التشغيل الحالية.",
        MessageKey.INVALID_REQUEST: "الطلب غير صالح.",
        MessageKey.UNAUTHENTICATED: "يجب تسجيل الدخول.",
        MessageKey.NOT_FOUND: "العنصر المطلوب غير موجود.",
        MessageKey.INTERNAL_ERROR: "حدث خطأ داخلي.",
    },
    Locale.EN: {
        MessageKey.RUN_STARTED: "Run started.",
        MessageKey.RUN_COMPLETED: "Task completed successfully.",
        MessageKey.RUN_FAILED: "Task failed.",
        MessageKey.APPROVAL_REQUIRED: "User approval is required to proceed.",
        MessageKey.APPROVAL_GRANTED: "Operation approved.",
        MessageKey.APPROVAL_DENIED: "Operation denied.",
        MessageKey.PERMISSION_DENIED: "This operation is not permitted.",
        MessageKey.TOOL_NOT_FOUND: "The requested tool is unavailable.",
        MessageKey.SPECIALIST_UNKNOWN: "The requested specialist is not known.",
        MessageKey.SPECIALIST_SCOPE_DENIED: "That tool is outside this agent's specialist scope.",
        MessageKey.SPECIALIST_DEGRADED: "Some tools for this specialist are unavailable here.",
        MessageKey.INVALID_REQUEST: "The request is invalid.",
        MessageKey.UNAUTHENTICATED: "Authentication is required.",
        MessageKey.NOT_FOUND: "The requested resource was not found.",
        MessageKey.INTERNAL_ERROR: "An internal error occurred.",
    },
}

_DIRECTIONS: dict[Locale, Direction] = {
    Locale.AR: Direction.RTL,
    Locale.EN: Direction.LTR,
}


def direction_for(locale: Locale) -> Direction:
    """Text direction for a locale."""
    return _DIRECTIONS.get(locale, Direction.LTR)


def translate(key: MessageKey, locale: Locale = Locale.AR) -> str:
    """Translate a message key, falling back to the key itself."""
    catalog = MESSAGES.get(locale) or MESSAGES[Locale.AR]
    return catalog.get(key) or key.value
