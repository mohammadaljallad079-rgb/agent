"""Goal-aware tool retrieval.

A weak local model cannot reliably choose between thirty-odd tools that are all
pasted into one prompt: it drifts to whichever generic tool it recognises first
(a PDF task becomes ``write_file`` rather than the specialised document tool).
The fix is not a bigger model but a smaller, relevant choice set — the same
argument the context manager already makes for history and files.

This module ranks the registry against the goal and returns the few tools worth
advertising, so the planner is choosing between plausible options. It is
deliberately lexical and dependency-free (no embeddings, no network): the
Arabic-first intent lexicon below carries the cross-lingual signal that a plain
token overlap would miss, because tool descriptions are written in English while
the goal arrives in Arabic.

The ranking is a hint, never a gate: the runner still falls back to the full
registry for a step whose hint is ``None``, so a tool this module failed to
surface remains reachable.
"""

from __future__ import annotations

import math
import re
from collections.abc import Sequence

from ..models.base import ToolSpec

# Tools that must always be offered: without ``finish`` the agent cannot end the
# run cleanly, and retrieval is about *which work tools* to show, not about
# hiding the control surface.
ALWAYS_INCLUDE: frozenset[str] = frozenset({"finish", "echo"})

_DEFAULT_MAX_TOOLS = 12

# Arabic-first intent lexicon. Keys are normalised query terms (Arabic or
# English); values are the canonical capability words that appear in English
# tool names and descriptions. Kept explicit and small on purpose: it is the one
# piece of cross-lingual knowledge the lexical ranker needs, and it is easy to
# review and extend.
_INTENT_LEXICON: dict[str, tuple[str, ...]] = {
    # documents
    "مستند": ("document", "pdf", "docx"),
    "وثيقة": ("document", "pdf"),
    "وثائق": ("document",),
    "تقرير": ("document", "report"),
    "تقارير": ("document", "report"),
    "pdf": ("pdf", "document"),
    "docx": ("docx", "document"),
    "word": ("docx", "document"),
    # presentations
    "عرض": ("presentation", "slides", "pptx"),
    "تقديمي": ("presentation", "slides"),
    "تقديمية": ("presentation", "slides"),
    "شرائح": ("presentation", "slides"),
    "سلايد": ("presentation", "slides"),
    "pptx": ("pptx", "presentation"),
    "powerpoint": ("pptx", "presentation"),
    # spreadsheets
    "جدول": ("spreadsheet", "xlsx", "csv"),
    "جداول": ("spreadsheet", "xlsx", "csv"),
    "اكسل": ("spreadsheet", "xlsx"),
    "إكسل": ("spreadsheet", "xlsx"),
    "xlsx": ("xlsx", "spreadsheet"),
    "excel": ("xlsx", "spreadsheet"),
    "csv": ("csv", "spreadsheet"),
    # files
    "ملف": ("file",),
    "ملفات": ("file",),
    "مجلد": ("directory", "folder"),
    "مجلدات": ("directory", "folder"),
    # communication
    "بريد": ("email",),
    "ايميل": ("email",),
    "إيميل": ("email",),
    "رسالة": ("message", "email"),
    "واتساب": ("whatsapp", "message"),
    "whatsapp": ("whatsapp",),
    # printing
    "طباعة": ("print", "printer"),
    "طابعة": ("print", "printer"),
    "print": ("print",),
    # browsing / web
    "متصفح": ("browser", "navigate"),
    "موقع": ("browser", "navigate", "http"),
    "رابط": ("http", "url", "browser"),
    "بحث": ("search", "research"),
    # enterprise / records
    "فاتورة": ("invoice", "record"),
    "فواتير": ("invoice", "record"),
    "عميل": ("customer", "record"),
    "عملاء": ("customer", "record"),
    "مبيعات": ("sales", "record"),
    "حساب": ("account", "record"),
    # calendar
    "موعد": ("calendar", "event"),
    "تقويم": ("calendar",),
    "اجتماع": ("calendar", "event"),
    # media / vision
    "صورة": ("image", "vision"),
    "صور": ("image", "vision"),
    "فيديو": ("video",),
    "video": ("video",),
    "صوت": ("audio", "speech"),
}

# Common words that carry no routing signal; dropping them keeps the generic
# file tools from winning on noise alone.
_STOPWORDS: frozenset[str] = frozenset(
    {
        "من",
        "في",
        "على",
        "الى",
        "إلى",
        "عن",
        "مع",
        "ثم",
        "هذا",
        "هذه",
        "ذلك",
        "التي",
        "الذي",
        "كل",
        "او",
        "أو",
        "و",
        "باسم",
        "يحتوي",
        "محتوى",
        "إنشاء",
        "انشاء",
        "انشئ",
        "أنشئ",
        "جهز",
        "جهّز",
        "اكتب",
        "احفظ",
        "حفظ",
        "the",
        "a",
        "an",
        "to",
        "of",
        "in",
        "on",
        "for",
        "and",
        "with",
        "that",
        "this",
        "create",
        "make",
        "generate",
        "write",
        "save",
        "please",
        "file",
    }
)

_ARABIC_DIACRITICS_RE = re.compile(r"[\u0610-\u061a\u064b-\u065f\u0670\u06d6-\u06ed]")
_WORD_RE = re.compile(r"[\w\u0600-\u06ff]+", re.UNICODE)


def _normalise_arabic(word: str) -> str:
    """Fold the orthographic variants that would otherwise split a keyword.

    Alef/hamza forms and ta marbuta are unified, the definite article is
    stripped, and diacritics are removed, so ``الإيميل`` and ``ايميل`` and
    ``إيميل`` all collapse to one lookup key.
    """
    word = _ARABIC_DIACRITICS_RE.sub("", word)
    # Written with escapes so the fold table is not read as ambiguous Latin
    # letters by the linter (RUF001): alef/hamza forms and ta marbuta unify,
    # and the definite article is stripped.
    word = (
        word.replace("\u0623", "\u0627")  # alef with hamza above
        .replace("\u0625", "\u0627")  # alef with hamza below
        .replace("\u0622", "\u0627")  # alef with madda
        .replace("\u0649", "\u064a")  # alef maksura
        .replace("\u0629", "\u0647")  # ta marbuta
        .replace("\u0624", "\u0648")  # waw with hamza
        .replace("\u0626", "\u064a")  # yeh with hamza
    )
    if len(word) > 3 and word.startswith("\u0627\u0644"):  # leading definite article
        word = word[2:]
    # Accusative tanwin leaves a trailing alef ("ايميلا" for "ايميلاً"); drop it
    # so the keyword still matches. A no-op for words that do not end in alef.
    if len(word) > 3 and word.endswith("\u0627"):
        word = word[:-1]
    return word


def _tokens(text: str) -> list[str]:
    tokens: list[str] = []
    for raw in _WORD_RE.findall(text.lower()):
        token = _normalise_arabic(raw)
        if len(token) < 2 or token in _STOPWORDS:
            continue
        tokens.append(token)
    return tokens


def _tool_terms(spec: ToolSpec) -> set[str]:
    """The searchable terms of a tool: name parts plus description words."""
    terms = set(_tokens(spec.name.replace("_", " ")))
    terms |= set(_tokens(spec.description or ""))
    return terms


def _expand(query_tokens: Sequence[str]) -> set[str]:
    expanded: set[str] = set(query_tokens)
    for token in query_tokens:
        expanded.update(_INTENT_LEXICON.get(token, ()))
    return expanded


def select_relevant_tools(
    goal: str,
    tool_specs: Sequence[ToolSpec],
    *,
    max_tools: int = _DEFAULT_MAX_TOOLS,
) -> tuple[ToolSpec, ...]:
    """Return the tools most relevant to ``goal``, in registry order.

    Ranking is IDF-weighted lexical overlap: a term shared by many tools (e.g.
    ``file``) contributes little, while a distinctive one (``pdf``,
    ``presentation``) contributes a lot, and a match in the tool's *name* counts
    triple. The always-include control tools are kept regardless of score, and
    the result is returned in the registry's order so the prompt stays stable
    across runs.
    """
    specs = list(tool_specs)
    if not specs:
        return ()
    if len(specs) <= max_tools:
        return tuple(specs)

    documents = {spec.name: _tool_terms(spec) for spec in specs}
    document_frequency: dict[str, int] = {}
    for terms in documents.values():
        for term in terms:
            document_frequency[term] = document_frequency.get(term, 0) + 1
    total = len(specs)

    def idf(term: str) -> float:
        return math.log(1 + total / document_frequency.get(term, total))

    query_terms = _expand(_tokens(goal))
    scored: list[tuple[float, int, ToolSpec]] = []
    for index, spec in enumerate(specs):
        name_terms = set(_tokens(spec.name.replace("_", " ")))
        description_terms = documents[spec.name]
        score = 0.0
        for term in query_terms:
            if term in name_terms:
                score += 3.0 * idf(term)
            elif term in description_terms:
                score += 1.0 * idf(term)
        if spec.name in ALWAYS_INCLUDE:
            score += 1000.0
        scored.append((score, index, spec))

    # Highest score first; ties keep registry order for a stable prompt.
    scored.sort(key=lambda item: (-item[0], item[1]))
    return tuple(spec for _, _, spec in scored[:max_tools])
