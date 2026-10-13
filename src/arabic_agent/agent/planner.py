"""Planner.

Turns a goal into a :class:`TaskPlan` using the model gateway. Parsing is
defensive: models sometimes wrap JSON in prose or code fences, so we extract the
first balanced JSON object. If planning fails entirely we fall back to a
single-step plan that simply asks the model to act — a genuine degraded mode,
not a fabricated plan.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Sequence
from typing import Any

from ..core.enums import Locale
from ..core.errors import PlatformError
from ..models.base import ChatMessage, ModelRequest, ToolSpec
from ..models.gateway import ModelGateway
from ..tasks.base import TaskPlan
from .prompts import planning_prompt

logger = logging.getLogger(__name__)

_JSON_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)
MAX_STEPS = 10


class Planner:
    """Produces executable task plans from natural-language goals."""

    def __init__(self, gateway: ModelGateway, *, locale: Locale = Locale.AR) -> None:
        self._gateway = gateway
        self._locale = locale

    async def plan(
        self,
        *,
        tenant_id: str,
        user_id: str,
        run_id: str,
        goal: str,
        tool_specs: Sequence[ToolSpec] = (),
        tool_char_budget: int | None = None,
    ) -> TaskPlan:
        descriptions, hints = await self._ask_model(
            goal, tool_specs=tool_specs, tool_char_budget=tool_char_budget
        )
        return TaskPlan.from_descriptions(
            tenant_id=tenant_id,
            user_id=user_id,
            run_id=run_id,
            goal=goal,
            descriptions=descriptions,
            tool_hints=hints,
        )

    async def _ask_model(
        self,
        goal: str,
        *,
        tool_specs: Sequence[ToolSpec] = (),
        tool_char_budget: int | None = None,
    ) -> tuple[list[str], list[str | None]]:
        request = ModelRequest(
            messages=(
                ChatMessage.system("You are a planning module. Output JSON only."),
                ChatMessage.user(
                    planning_prompt(
                        self._locale,
                        goal,
                        tool_specs=tool_specs,
                        max_tool_chars=tool_char_budget,
                    )
                ),
            ),
            temperature=0.0,
        )
        try:
            response = await self._gateway.complete(request)
        except PlatformError as exc:
            logger.warning("planning model call failed: %s", exc.message)
            return [goal], [None]

        parsed = self._parse(
            response.content,
            valid_tools={s.name for s in tool_specs} if tool_specs else None,
        )
        if parsed is None:
            return [goal], [None]
        return parsed

    @staticmethod
    def _parse(
        content: str, *, valid_tools: set[str] | None = None
    ) -> tuple[list[str], list[str | None]] | None:
        match = _JSON_OBJECT_RE.search(content or "")
        if not match:
            return None
        try:
            payload: Any = json.loads(match.group(0))
        except json.JSONDecodeError:
            return None
        steps_raw = payload.get("steps") if isinstance(payload, dict) else None
        if not isinstance(steps_raw, list) or not steps_raw:
            return None
        descriptions: list[str] = []
        hints: list[str | None] = []
        for item in steps_raw[:MAX_STEPS]:
            if isinstance(item, str):
                descriptions.append(item)
                hints.append(None)
            elif isinstance(item, dict):
                description = str(item.get("description") or "").strip()
                if not description:
                    continue
                descriptions.append(description)
                hint = item.get("tool_hint")
                hint = str(hint) if hint else None
                # A hint naming a tool that does not exist is dropped, not
                # trusted: advertising a fabricated tool would make the step
                # fall back to the full registry, and the runner would later
                # treat the plan as authoritative. No hint is the honest state.
                if hint is not None and valid_tools is not None and hint not in valid_tools:
                    logger.debug("planner produced unknown tool hint %r; dropping it", hint)
                    hint = None
                hints.append(hint)
        if not descriptions:
            return None
        return descriptions, hints
