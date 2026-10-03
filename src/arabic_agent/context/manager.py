"""Context manager.

The platform must never dump every file, memory and tool result into the model
on each turn. This manager assembles a minimal, prioritised context under a
token budget: identity and policy first (non-negotiable), then the current goal
and plan, then recent turns, then the most relevant memories, then tool
results — truncating the least important material first.
"""

from __future__ import annotations

from collections.abc import Sequence

from pydantic import BaseModel, ConfigDict, Field

from ..core.config import Settings
from ..core.enums import Locale
from ..memory.base import MemoryEntry
from ..models.base import ChatMessage, ToolSpec
from ..tasks.base import TaskPlan
from .budget import TokenBudget, estimate_tokens, normalize_text

#: Fraction of the window reserved for the model's own output.
_OUTPUT_RESERVE = 0.15


class ContextPackage(BaseModel):
    """The assembled context handed to the model for one turn."""

    model_config = ConfigDict(frozen=True)

    messages: tuple[ChatMessage, ...]
    tools: tuple[ToolSpec, ...]
    budget: dict[str, int] = Field(default_factory=dict)
    dropped: tuple[str, ...] = ()
    truncated: bool = False


class ContextManager:
    """Builds a budgeted context for a single model call."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def build(
        self,
        *,
        system_prompt: str,
        goal: str,
        plan: TaskPlan | None,
        recent_messages: Sequence[ChatMessage],
        memories: Sequence[MemoryEntry] = (),
        tool_specs: Sequence[ToolSpec] = (),
        scratchpad: str = "",
        locale: Locale = Locale.AR,
    ) -> ContextPackage:
        total = self._settings.max_context_tokens
        output_reserve = int(total * _OUTPUT_RESERVE)
        budget = TokenBudget(total=total - output_reserve)
        dropped: list[str] = []
        truncated = False

        # 1. System prompt is non-negotiable.
        system_text = normalize_text(system_prompt)
        budget.force_reserve("system", estimate_tokens(system_text))

        # 2. Tool schemas are part of the contract; reserve but allow trimming
        #    of long descriptions if the budget is tight.
        tool_text = "\n".join(f"{t.name}: {t.description}" for t in tool_specs)
        tool_tokens = estimate_tokens(tool_text)
        if not budget.reserve("tools", tool_tokens):
            # Keep the tool names only; a degraded but usable fallback.
            trimmed = "\n".join(t.name for t in tool_specs)
            trimmed_tokens = estimate_tokens(trimmed)
            budget.reserve("tools", trimmed_tokens)
            truncated = True

        # 3. Goal + current plan.
        goal_text = normalize_text(goal)
        budget.force_reserve("goal", estimate_tokens(goal_text))
        if plan is not None:
            plan_text = normalize_text(plan.render())
            if not budget.reserve("plan", estimate_tokens(plan_text)):
                dropped.append("plan")
                truncated = True

        # 4. Relevant memories (already ranked by the store; drop lowest first).
        memory_lines: list[str] = []
        for entry in memories:
            line = normalize_text(f"[{entry.kind.value}] {entry.key}: {entry.content}")
            cost = estimate_tokens(line)
            if budget.reserve("memory", cost):
                memory_lines.append(line)
            else:
                dropped.append(f"memory:{entry.key}")
                truncated = True

        # 5. Scratchpad / intermediate notes.
        if scratchpad:
            scratch = normalize_text(scratchpad)
            if not budget.reserve("scratchpad", estimate_tokens(scratch)):
                dropped.append("scratchpad")
                truncated = True

        # 6. Recent conversation, newest-first, until the budget is exhausted.
        kept: list[ChatMessage] = []
        for message in reversed(recent_messages):
            cost = estimate_tokens(message.content)
            if budget.reserve("history", cost):
                kept.append(message)
            else:
                dropped.append("history")
                truncated = True
                break
        kept.reverse()

        messages: list[ChatMessage] = [
            ChatMessage.system(
                self._compose_system(system_text, goal_text, plan, memory_lines, locale)
            )
        ]
        messages.extend(kept)

        return ContextPackage(
            messages=tuple(messages),
            tools=tuple(tool_specs),
            budget={**budget.sections, "total": total, "used": budget.used},
            dropped=tuple(dropped),
            truncated=truncated,
        )

    @staticmethod
    def _compose_system(
        system_text: str,
        goal_text: str,
        plan: TaskPlan | None,
        memory_lines: Sequence[str],
        locale: Locale,
    ) -> str:
        parts = [system_text]
        parts.append(f"\n\n# Current goal\n{goal_text}")
        if plan is not None:
            parts.append(f"\n\n# Current plan\n{plan.render()}")
        if memory_lines:
            parts.append("\n\n# Relevant memory\n" + "\n".join(memory_lines))
        direction = (
            "Text direction is right-to-left."
            if locale is Locale.AR
            else "Text direction is left-to-right."
        )
        parts.append(f"\n\n# Language\nRespond in {locale.value}. {direction}")
        return "".join(parts)
