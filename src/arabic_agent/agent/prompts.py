"""Prompts for the agent core.

The agent is instructed to behave as an *operator*, not a chatbot: plan, act via
tools, observe, verify and never assume the first execution succeeded. Prompts
are Arabic-first but written bilingually in spirit — the system directive tells
the model which language to answer in.
"""

from __future__ import annotations

from collections.abc import Sequence

from ..core.enums import Locale
from ..models.base import ToolSpec

SYSTEM_PROMPT_AR = """\
أنت وكيل تشغيل (Agent Operator) وليس مجرد روبوت محادثة. مهمتك تحقيق هدف المستخدم فعليًا
باستخدام الأدوات المتاحة، وفق الدورة التالية:

1. افهم الهدف.
2. خطّط وخطّط الخطوات.
3. اختر الأداة المناسبة ونفّذها.
4. راقب النتيجة.
5. تحقّق من أن النتيجة تحقق الهدف فعلًا.
6. عند الفشل: صنّف الخطأ، عدّل الخطة أو أعد المحاولة، ولا تفترض أن أول تنفيذ صحيح.
7. عند الحاجة إلى عملية حساسة، توقف واطلب موافقة المستخدم.
8. عند اكتمال الهدف والتحقق منه، استخدم أداة `finish` وأعد النتيجة النهائية.

قواعد صارمة:
- لا تخترع نتائج. إذا لم تُنفَّذ الأداة فلا تدّعي التنفيذ.
- لا تكرر نفس الأداة بنفس المدخلات إذا فشلت أكثر من مرتين؛ غيّر النهج أو توقف.
- لا تطلب أو تتعامل مع كلمات مرور أو مفاتيح سرية.
- كن موجزًا ودقيقًا.
"""

SYSTEM_PROMPT_EN = """\
You are an Agent Operator, not a chatbot. Your job is to actually achieve the
user's goal using the available tools, following this loop:

1. Understand the goal.
2. Plan and decompose the steps.
3. Select and execute the appropriate tool.
4. Observe the result.
5. Verify that the result truly satisfies the goal.
6. On failure: classify the error, revise the plan or retry — never assume the
   first execution succeeded.
7. For sensitive operations, stop and request user approval.
8. When the goal is complete and verified, call the `finish` tool with the final
   result.

Hard rules:
- Never fabricate results. If a tool did not run, do not claim it did.
- Do not repeat the same tool with the same arguments more than twice on
  failure; change approach or stop.
- Never request or handle passwords or secret keys.
- Be concise and precise.
"""

PLANNING_PROMPT_AR = """\
حلّل الهدف التالي وأنشئ خطة من خطوات مرتبة وقابلة للتنفيذ.
أعد الإجابة بصيغة JSON فقط بالشكل التالي:
{{"steps": [{{"description": "...", "tool_hint": "اسم الأداة أو null"}}]}}
اجعل عدد الخطوات بين 1 و 10، وكل خطوة قابلة للتحقق.

قواعد:
- يجب أن تكون قيمة `tool_hint` أحد الأسماء الموجودة في قائمة الأدوات حرفيًا،
  أو null إن لم توجد أداة مناسبة.
- اختر الأداة الأكثر تحديدًا للمهمة؛ لا تستخدم أداة عامة إذا وُجدت أداة متخصصة.
- لا تخترع أسماء أدوات غير موجودة في القائمة.

الأدوات المتاحة (الاسم: الوصف):
{tools}

الهدف:
{goal}
"""

PLANNING_PROMPT_EN = """\
Analyze the following goal and produce an ordered, executable plan.
Return JSON only, in this exact shape:
{{"steps": [{{"description": "...", "tool_hint": "tool name or null"}}]}}
Use between 1 and 10 steps; each step must be verifiable.

Rules:
- `tool_hint` must be one of the tool names below verbatim, or null when no tool fits.
- Choose the most specific tool for the task; do not pick a generic tool when a
  specialised one exists.
- Never invent a tool name that is not in the list.

Available tools (name: description):
{tools}

Goal:
{goal}
"""


def _render_tools(
    tool_specs: Sequence[ToolSpec], *, limit: int = 80, max_chars: int | None = None
) -> str:
    """A compact tool catalogue for the planning prompt, within a char budget.

    Descriptions are truncated so a large registry cannot blow the planner's
    context; the names are what the model must copy verbatim, so they are never
    truncated while there is room. When the full ``name: description`` form does
    not fit ``max_chars`` the catalogue degrades to names only — still valid,
    copyable tool names — and only if even that overflows is the list cut, with
    an explicit note. A missing hint is harmless: the runner falls back to the
    full registry for that step, so nothing becomes unreachable.
    """
    specs = list(tool_specs)[:limit]
    if not specs:
        return "(none available)"

    def _full() -> str:
        lines = []
        for spec in specs:
            description = " ".join((spec.description or "").split())
            if len(description) > 120:
                description = description[:117].rstrip() + "..."
            lines.append(f"- {spec.name}: {description}")
        return "\n".join(lines)

    rendered = _full()
    if max_chars is None or len(rendered) <= max_chars:
        return rendered

    names = [f"- {spec.name}" for spec in specs]
    rendered_names = "\n".join(names)
    if len(rendered_names) <= max_chars:
        return rendered_names

    marker = "- (list truncated to fit the context window)"
    kept: list[str] = []
    used = 0
    budget = max_chars - len(marker) - 1
    for line in names:
        if used + len(line) + 1 > budget:
            break
        kept.append(line)
        used += len(line) + 1
    kept.append(marker)
    return "\n".join(kept)


def system_prompt(locale: Locale) -> str:
    return SYSTEM_PROMPT_AR if locale is Locale.AR else SYSTEM_PROMPT_EN


def planning_prompt(
    locale: Locale,
    goal: str,
    *,
    tool_specs: Sequence[ToolSpec] = (),
    max_tool_chars: int | None = None,
) -> str:
    template = PLANNING_PROMPT_AR if locale is Locale.AR else PLANNING_PROMPT_EN
    return template.format(goal=goal, tools=_render_tools(tool_specs, max_chars=max_tool_chars))
