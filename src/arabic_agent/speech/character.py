"""The SUPER AI agent character — one definition, many surfaces.

The product has a *character*, not a mascot sticker: a named identity with a
persona, a voice profile and a visual specification that the API, the desktop UI
and any future presenter all read from this single source. That is what keeps the
character consistent when the agent speaks in the desktop app, appears in a video
the studio composes, or is described by the API — instead of three drifting
copies.

It is deliberately *data*, not code: a :class:`Character` is a frozen model with
no runtime dependency, so the runner can read its persona directive, the desktop
bridge can serialise it to the page, and a test can assert its shape without
building a platform. The Arabic text is authored first — the character's native
language is Arabic, and its persona directive is written so the model answers in
Arabic by default while still switching cleanly to English.

The character is the *presenter* of the general-purpose SUPER AI AGENT. It does
not restrict what the agent can do — the agent is still the whole registry — it
gives that agent a consistent voice and face.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from ..core.enums import Locale

#: The character's identifier. Stable, used as a key by the API and the UI.
SUPER_AI = "super_ai"

#: The persona directive appended to the shared operator prompt when the SUPER AI
#: character is active. Written in Arabic first (the product's primary market),
#: with an explicit instruction to answer in the user's language.
_SUPER_AI_PERSONA_AR = (
    "أنت «المساعد الذكي الخارق»، الوكيل العام لمنصة الذكاء الاصطناعي العربية. "
    "تتحدث العربية الفصحى المبسّطة افتراضيًا، وتنتقل بسلاسة إلى لغة المستخدم عند مخاطبتك بها. "
    "أسلوبك: واثق، موجز، عملي، ومهني. تخطّط قبل التنفيذ، تستخدم الأدوات فعليًا، "
    "تتحقق من النتائج، وتُبلّغ عن الأخطاء بصراحة بدلًا من ادعاء النجاح. "
    "لا تدّعي تنفيذ عملية لم تنفّذها، وتطلب موافقة المستخدم قبل أي إجراء حساس."
)

_SUPER_AI_PERSONA_EN = (
    'You are "The Super AI Assistant", the general-purpose agent of the Arabic AI '
    "platform. You speak Modern Standard Arabic by default and switch to the user's "
    "language when addressed in it. Your style is confident, concise, practical and "
    "professional. You plan before acting, use tools for real, verify results, and "
    "report failures honestly instead of claiming success. Never claim an action you "
    "did not take, and ask for approval before any sensitive operation."
)


class VoiceProfile(BaseModel):
    """How the character sounds when it speaks.

    ``preferred_voice`` is a *hint* the synthesis engine may honour; when the
    engine has no matching voice it uses its default rather than failing, and the
    capability report says so.
    """

    model_config = ConfigDict(frozen=True)

    language: str = "ar"
    preferred_voice: str = "ar-female-warm"
    rate: float = 1.0
    pitch: float = 1.0
    #: Extra voice-name fragments the engines map onto their own voice ids, in
    #: preference order. Kept as plain strings so no engine is hard-coded here.
    voice_hints: tuple[str, ...] = ("ar", "arabic", "female", "warm")


class Viseme(BaseModel):
    """One mouth shape and the phonemes that trigger it.

    A viseme map is what lets a presenter animate the character's mouth from the
    same text it speaks, without a second animation engine. It is plain data so
    the desktop UI and a video compositor can share it.
    """

    model_config = ConfigDict(frozen=True)

    shape: str
    phonemes: tuple[str, ...] = ()


#: A compact, Arabic-aware viseme set. Arabic has distinctive emphatic and
#: pharyngeal consonants, so the map keeps them separate rather than collapsing
#: them into Latin buckets — the mouth shape should reflect the sound.
VISEMES: tuple[Viseme, ...] = (
    Viseme(shape="closed", phonemes=("m", "b", "p", "م", "ب", "پ")),
    Viseme(shape="open", phonemes=("a", "e", "ا", "َ", "أ", "ه")),  # noqa: RUF001
    Viseme(shape="wide", phonemes=("i", "y", "ي", "ِ")),
    Viseme(shape="round", phonemes=("u", "o", "w", "و", "ُ", "ؤ")),
    Viseme(shape="teeth", phonemes=("f", "v", "ف", "ث", "س", "ز")),
    Viseme(shape="tongue", phonemes=("l", "n", "t", "d", "ل", "ن", "ت", "د", "ط", "ض")),
    Viseme(shape="throat", phonemes=("h", "k", "g", "ق", "ك", "خ", "غ", "ح", "ع")),
    Viseme(shape="emphatic", phonemes=("ص", "ض", "ط", "ظ")),
    Viseme(shape="trill", phonemes=("r", "ر")),
    Viseme(shape="sh", phonemes=("sh", "ch", "j", "ش", "ج")),
)

#: Visual states the presenter can render, so a UI and a video share vocabulary.
VISUAL_STATES: tuple[str, ...] = (
    "idle",
    "listening",
    "thinking",
    "speaking",
    "waiting_approval",
    "error",
    "success",
)


class Character(BaseModel):
    """A complete, reusable agent identity.

    Every field is data. The persona directive is what the runner injects into
    the system prompt; the voice and visual profiles are what a presenter reads.
    """

    model_config = ConfigDict(frozen=True)

    id: str
    name_ar: str
    name_en: str
    tagline_ar: str
    tagline_en: str
    persona_ar: str
    persona_en: str
    default_locale: Locale = Locale.AR
    voice: VoiceProfile = VoiceProfile()
    #: Brand colours as CSS hex strings, so the UI has one source of truth.
    primary_color: str = "#0f766e"
    accent_color: str = "#f59e0b"
    background_color: str = "#0b1220"
    #: Viseme map the presenter uses to animate speech.
    visemes: tuple[Viseme, ...] = VISEMES
    #: Visual states the presenter understands.
    states: tuple[str, ...] = VISUAL_STATES

    def persona(self, locale: Locale | None = None) -> str:
        """The persona directive for a locale (Arabic by default)."""
        chosen = locale or self.default_locale
        return self.persona_en if chosen is Locale.EN else self.persona_ar

    def name(self, locale: Locale | None = None) -> str:
        chosen = locale or self.default_locale
        return self.name_en if chosen is Locale.EN else self.name_ar

    def to_public_dict(self) -> dict[str, object]:
        """A UI-safe view: identity, style and animation data, no persona prompt.

        The persona directive is an internal instruction to the model, not
        something the page needs; exposing it would invite a page to depend on
        prompt internals. The public view carries everything a presenter needs.
        """
        return {
            "id": self.id,
            "name_ar": self.name_ar,
            "name_en": self.name_en,
            "tagline_ar": self.tagline_ar,
            "tagline_en": self.tagline_en,
            "default_locale": self.default_locale.value,
            "voice": self.voice.model_dump(mode="json"),
            "primary_color": self.primary_color,
            "accent_color": self.accent_color,
            "background_color": self.background_color,
            "visemes": [v.model_dump(mode="json") for v in self.visemes],
            "states": list(self.states),
        }


SUPER_AI_CHARACTER = Character(
    id=SUPER_AI,
    name_ar="المساعد الذكي الخارق",
    name_en="Super AI Assistant",
    tagline_ar="وكيلك الذكي الذي يخطّط وينفّذ ويتحقق — لا مجرّد محادثة.",
    tagline_en="Your agent that plans, executes and verifies — not just a chat.",
    persona_ar=_SUPER_AI_PERSONA_AR,
    persona_en=_SUPER_AI_PERSONA_EN,
    default_locale=Locale.AR,
)

#: Every character the platform knows. One today; the registry shape lets a
#: tenant pick a different presenter later without touching the agent core.
_CHARACTERS: dict[str, Character] = {SUPER_AI_CHARACTER.id: SUPER_AI_CHARACTER}

DEFAULT_CHARACTER = SUPER_AI_CHARACTER


def characters() -> tuple[Character, ...]:
    """Every registered character."""
    return tuple(_CHARACTERS.values())


def get_character(character_id: str | None) -> Character:
    """Return a character by id, falling back to the default.

    Unknown ids fall back rather than raising: the character is presentation, and
    a stale id from an old client should not break a run. The id used is echoed
    in the run's audit event, so a fallback is visible, not silent.
    """
    if not character_id:
        return DEFAULT_CHARACTER
    return _CHARACTERS.get(character_id, DEFAULT_CHARACTER)


__all__ = [
    "DEFAULT_CHARACTER",
    "SUPER_AI",
    "SUPER_AI_CHARACTER",
    "VISEMES",
    "VISUAL_STATES",
    "Character",
    "Viseme",
    "VoiceProfile",
    "characters",
    "get_character",
]
