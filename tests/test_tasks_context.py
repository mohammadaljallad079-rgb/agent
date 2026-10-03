"""Task engine and context manager tests."""

from __future__ import annotations

from arabic_agent.context.budget import estimate_tokens
from arabic_agent.context.manager import ContextManager
from arabic_agent.core.enums import Locale, StepStatus
from arabic_agent.models.base import ChatMessage, ToolSpec
from arabic_agent.tasks.base import TaskPlan
from arabic_agent.tasks.engine import TaskEngine
from arabic_agent.tasks.store import InMemoryTaskStore


def _plan() -> TaskPlan:
    return TaskPlan.from_descriptions(
        tenant_id="tnt_1",
        user_id="usr_1",
        run_id="run_1",
        goal="جهز الفواتير",
        descriptions=["جمع العملاء", "إنشاء الفواتير", "إرسال"],
        tool_hints=["echo", None, None],
    )


async def test_step_lifecycle_is_persisted():
    engine = TaskEngine(InMemoryTaskStore())
    plan = await engine.create_plan(_plan())
    step = plan.current_step()
    assert step is not None and step.index == 0

    plan = await engine.start_step(plan, step.id)
    assert plan.current_step().status is StepStatus.IN_PROGRESS

    plan = await engine.complete_step(plan, step.id, summary="done")
    assert plan.steps[0].status is StepStatus.DONE
    assert plan.current_step().index == 1


async def test_plan_is_resumable_after_interruption():
    store = InMemoryTaskStore()
    engine = TaskEngine(store)
    plan = await engine.create_plan(_plan())
    first = plan.current_step()
    plan = await engine.complete_step(plan, first.id, summary="ok")

    reloaded = await engine.load_by_run(tenant_id="tnt_1", run_id="run_1")
    assert reloaded is not None
    assert reloaded.steps[0].status is StepStatus.DONE
    assert reloaded.current_step().index == 1


async def test_replan_preserves_completed_steps():
    engine = TaskEngine(InMemoryTaskStore())
    plan = await engine.create_plan(_plan())
    first = plan.current_step()
    plan = await engine.complete_step(plan, first.id, summary="ok")

    from arabic_agent.tasks.base import TaskStep

    revised = await engine.replan(
        plan,
        new_steps=[TaskStep(index=0, description="نهج بديل")],
        reason="recovery",
    )
    assert revised.revision == 1
    assert revised.steps[0].status is StepStatus.DONE
    assert revised.steps[1].description == "نهج بديل"


def test_arabic_text_estimates_more_tokens_than_latin():
    arabic = estimate_tokens("مرحبا بكم في المنصة العربية للوكلاء الأذكياء")
    latin = estimate_tokens("hello and welcome to the arabic agent platform")
    # Same-ish length, but Arabic is tokenised less efficiently → more tokens.
    assert arabic > latin


def test_context_budget_drops_history_when_exceeded(settings):
    manager = ContextManager(settings)
    messages = [ChatMessage.user("س" * 500) for _ in range(50)]
    package = manager.build(
        system_prompt="system",
        goal="goal",
        plan=None,
        recent_messages=messages,
        tool_specs=(ToolSpec(name="echo", description="echo"),),
        locale=Locale.AR,
    )
    assert package.truncated
    assert "history" in package.dropped
    assert package.budget["used"] <= package.budget["total"]


def test_context_includes_goal_and_language(settings):
    manager = ContextManager(settings)
    package = manager.build(
        system_prompt="system",
        goal="أنشئ فاتورة",
        plan=None,
        recent_messages=[],
        locale=Locale.AR,
    )
    system = package.messages[0].content
    assert "أنشئ فاتورة" in system
    assert "right-to-left" in system
