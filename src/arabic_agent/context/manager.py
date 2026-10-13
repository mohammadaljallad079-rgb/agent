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
from .attachments import ContextAttachment
from .budget import TokenBudget, estimate_tokens, estimate_tool_schema, normalize_text

#: Fraction of the window reserved for the model's own output.
_OUTPUT_RESERVE = 0.15
#: Default ceiling on advertised tools when settings do not specify one.
_DEFAULT_MAX_TOOLS = 64


def _tool_cost(spec: ToolSpec) -> int:
    """Estimated tokens a tool schema adds to the request.

    Counts the full serialised function object (wrapper + JSON-schema
    ``parameters``), matching what providers actually send.
    """
    return estimate_tool_schema(spec.name, spec.description, spec.parameters)


#: Tools the agent needs to operate at all; always advertised. ``inspect_project``
#: and ``inspect_document`` let it discover which richer tools exist, so they are
#: part of the core set rather than droppable.
CORE_TOOLS = frozenset(
    {
        "finish",
        "echo",
        "read_file",
        "write_file",
        "list_directory",
        "run_command",
        "inspect_project",
        "inspect_document",
    }
)


def _split_tool_specs(
    specs: Sequence[ToolSpec],
) -> tuple[tuple[ToolSpec, ...], tuple[ToolSpec, ...]]:
    """Partition specs into (core, optional), preserving the given order."""
    core = tuple(s for s in specs if s.name in CORE_TOOLS)
    optional = tuple(s for s in specs if s.name not in CORE_TOOLS)
    return core, optional


class ContextPackage(BaseModel):
    """The assembled context handed to the model for one turn."""

    model_config = ConfigDict(frozen=True)

    messages: tuple[ChatMessage, ...]
    tools: tuple[ToolSpec, ...]
    budget: dict[str, int] = Field(default_factory=dict)
    dropped: tuple[str, ...] = ()
    truncated: bool = False
    #: Image attachments actually included (empty for a text-only model).
    attachments: tuple[ContextAttachment, ...] = ()


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
        attachments: Sequence[ContextAttachment] = (),
        allow_images: bool = False,
        window: int | None = None,
    ) -> ContextPackage:
        # A fixed-size local model (*window*) caps the budget to its real token
        # limit, regardless of the (cloud-sized) configured default. Nothing is
        # ever planned beyond what the provider can accept, so a small n_ctx is
        # used to its full extent instead of failing the call at the last moment.
        total = self._settings.max_context_tokens
        if window is not None and window > 0:
            total = min(total, int(window))
        output_reserve = int(total * _OUTPUT_RESERVE)
        if window is not None and window > 0:
            # A fixed local window is guarded by the inference backend, which
            # refuses a prompt unless ``prompt + reserved_output < window`` with
            # ``reserved_output = max(64, window // 8)``. Reserve at least that
            # much _plus_ framing margin, so the message overhead the estimator
            # does not itemise can never push a composed prompt over the guard.
            output_reserve = max(output_reserve, window // 8 + 128)
        budget = TokenBudget(total=total - output_reserve)
        dropped: list[str] = []
        truncated = False

        # 1. System prompt is non-negotiable.
        system_text = normalize_text(system_prompt)
        budget.force_reserve("system", estimate_tokens(system_text))

        # 2. Tool schemas are part of the contract. Advertise the core tools
        #    plus as many optional tools as the budget allows (counted with
        #    their full JSON schema), so a small local window is never
        #    overflowed. Core tools are always present; optional ones may be
        #    dropped, and the drop is reported.
        core, optional = _split_tool_specs(tool_specs)
        selected: list[ToolSpec] = []
        for spec in core:
            cost = _tool_cost(spec)
            if budget.reserve("tools", cost):
                selected.append(spec)
            else:
                budget.force_reserve("tools", cost)
                selected.append(spec)
                truncated = True
        max_tools = self._settings.max_tools_per_call or _DEFAULT_MAX_TOOLS
        for spec in optional:
            if len(selected) >= max_tools:
                dropped.append(f"tool:{spec.name}")
                truncated = True
                continue
            cost = _tool_cost(spec)
            if budget.reserve("tools", cost):
                selected.append(spec)
            else:
                dropped.append(f"tool:{spec.name}")
                truncated = True

        # 3. Goal + current plan.
        goal_text = normalize_text(goal)
        budget.force_reserve("goal", estimate_tokens(goal_text))
        include_plan = plan is not None
        if plan is not None:
            plan_text = normalize_text(plan.render())
            if not budget.reserve("plan", estimate_tokens(plan_text)):
                # The reservation failed, so the plan is not part of the budget.
                # It must also be omitted from the composed system message —
                # otherwise it is charged once for nothing while still consuming
                # real tokens, silently pushing the request past the window.
                include_plan = False
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

        # 7. Image attachments — only for a model that can actually see them.
        #    Each image is charged against the budget; one that does not fit is
        #    dropped rather than silently overflowing the window.
        included: list[ContextAttachment] = []
        if allow_images and attachments:
            for attachment in attachments:
                if budget.reserve("images", attachment.estimated_tokens):
                    included.append(attachment)
                else:
                    dropped.append(f"image:{attachment.source or attachment.kind}")
                    truncated = True

        messages: list[ChatMessage] = [
            ChatMessage.system(
                self._compose_system(
                    system_text,
                    goal_text,
                    plan if include_plan else None,
                    memory_lines,
                    locale,
                )
            )
        ]
        messages.extend(kept)
        if included:
            messages.append(
                ChatMessage.user(
                    "Attached screen capture(s) for the current step: "
                    + ", ".join(a.description or a.kind for a in included),
                    images=tuple(a.image for a in included),
                )
            )

        return ContextPackage(
            messages=tuple(messages),
            tools=tuple(selected),
            budget={**budget.sections, "total": total, "used": budget.used},
            dropped=tuple(dropped),
            truncated=truncated,
            attachments=tuple(included),
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
