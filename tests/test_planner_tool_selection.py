"""The planner must see the tools, and must not trust a fabricated hint.

A planning model can only choose a tool it has been told about. Before this
behaviour was added the planner was asked for a ``tool_hint`` while being shown
no tool list at all, so a real model answered ``null`` for every step and the
agent fell back to advertising the whole registry — exactly the condition that
lets a small local model pick an irrelevant tool.

These are unit tests over the real planner and prompt builder with a
deterministic fake provider; the opt-in real-model proof lives in
``test_local_agent_loop.py``.
"""

from __future__ import annotations

from collections.abc import Callable

from arabic_agent.agent.planner import Planner
from arabic_agent.agent.prompts import _render_tools, planning_prompt
from arabic_agent.core.config import Settings
from arabic_agent.core.enums import Locale
from arabic_agent.models.base import ModelRequest, ModelResponse, ToolSpec
from arabic_agent.models.gateway import ModelGateway
from arabic_agent.models.registry import ProviderRegistry

from .helpers import FakeProvider, plan_json, text_response

Responder = Callable[[ModelRequest, int], ModelResponse]


def _spec(name: str, description: str = "does a thing") -> ToolSpec:
    return ToolSpec(name=name, description=description, parameters={"type": "object"})


def _planner(responder: Responder) -> Planner:
    registry = ProviderRegistry()
    registry.register(FakeProvider(responder))
    gateway = ModelGateway(
        registry, settings=Settings(environment="test"), default_model="fake/model"
    )
    return Planner(gateway, locale=Locale.EN)


# --------------------------------------------------------------------------
# Prompt rendering.
# --------------------------------------------------------------------------
def test_render_tools_lists_every_name_with_a_description():
    specs = [_spec("create_presentation", "make slides"), _spec("write_file", "write a file")]
    rendered = _render_tools(specs)
    assert "- create_presentation: make slides" in rendered
    assert "- write_file: write a file" in rendered


def test_render_tools_truncates_long_descriptions_but_not_names():
    long_name = "a_tool_with_a_deliberately_long_but_valid_name"
    rendered = _render_tools([_spec(long_name, "x" * 400)])
    assert long_name in rendered
    assert "..." in rendered
    assert len(rendered) < 400


def test_render_tools_says_none_when_empty():
    assert _render_tools([]) == "(none available)"


def test_render_tools_degrades_to_names_within_a_budget():
    specs = [_spec(f"tool_{i}", "a fairly long description " * 10) for i in range(40)]
    rendered = _render_tools(specs, max_chars=800)
    assert len(rendered) <= 800
    # Names survive; descriptions are the first thing dropped.
    assert "tool_0" in rendered
    assert "a fairly long description" not in rendered


def test_render_tools_marks_a_hard_truncation():
    specs = [_spec(f"tool_with_a_long_name_{i:03d}") for i in range(200)]
    rendered = _render_tools(specs, max_chars=400)
    assert len(rendered) <= 400
    assert "truncated" in rendered


def test_planning_prompt_embeds_the_tools_and_the_goal():
    prompt = planning_prompt(Locale.AR, "أنشئ ملف PDF", tool_specs=[_spec("generate_document")])
    assert "generate_document" in prompt
    assert "أنشئ ملف PDF" in prompt


# --------------------------------------------------------------------------
# Hint validation.
# --------------------------------------------------------------------------
async def test_planner_drops_a_hallucinated_tool_hint():
    # The model invents a tool that does not exist; the planner must not pass
    # the fabricated name through as an authoritative hint.
    planner = _planner(
        lambda req, n: text_response(plan_json(("step one", "make_pdf_magic"), ("step two", None)))
    )
    plan = await planner.plan(
        tenant_id="t", user_id="u", run_id="r", goal="g", tool_specs=[_spec("generate_document")]
    )
    assert plan.steps[0].tool_hint is None
    assert plan.steps[1].tool_hint is None


async def test_planner_keeps_a_valid_tool_hint():
    planner = _planner(
        lambda req, n: text_response(plan_json(("make the deck", "create_presentation")))
    )
    plan = await planner.plan(
        tenant_id="t",
        user_id="u",
        run_id="r",
        goal="g",
        tool_specs=[_spec("create_presentation"), _spec("write_file")],
    )
    assert plan.steps[0].tool_hint == "create_presentation"


async def test_planner_without_a_catalogue_keeps_hints_unvalidated():
    # No specs supplied (e.g. an embedded caller) preserves the old behaviour:
    # the hint is passed through, because there is no catalogue to check against.
    planner = _planner(lambda req, n: text_response(plan_json(("s", "anything"))))
    plan = await planner.plan(tenant_id="t", user_id="u", run_id="r", goal="g")
    assert plan.steps[0].tool_hint == "anything"


async def test_planner_shows_the_model_the_tool_names():
    seen: list[str] = []

    def responder(request: ModelRequest, n: int) -> ModelResponse:
        seen.append(request.messages[-1].content)
        return text_response(plan_json(("s", None)))

    planner = _planner(responder)
    await planner.plan(
        tenant_id="t",
        user_id="u",
        run_id="r",
        goal="build a spreadsheet",
        tool_specs=[_spec("create_spreadsheet")],
    )
    assert "create_spreadsheet" in seen[0]
    assert "build a spreadsheet" in seen[0]
