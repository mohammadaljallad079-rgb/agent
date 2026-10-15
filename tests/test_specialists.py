"""Specialist framework tests.

Exercise the real catalogue, runner scoping, executor guard and API surface.
Only the LLM is a deterministic fake (see ``helpers``); everything else — the
agent loop, policy engine, executor and audit log — is the production code.
"""

from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient

from arabic_agent.api.app import create_app
from arabic_agent.container import Platform
from arabic_agent.core.enums import RunStatus
from arabic_agent.core.errors import NotFoundError
from arabic_agent.models.registry import ProviderRegistry
from arabic_agent.permissions.base import Principal
from arabic_agent.specialists import (
    BUILTIN_SPECIALISTS,
    CODING,
    DEFAULT_SPECIALIST,
    GENERAL,
    VIDEO,
    Specialist,
    default_catalogue,
)

from .helpers import FakeProvider, plan_json, scripted_responder, verify_json


def _principal(**kwargs: Any) -> Principal:
    defaults: dict[str, Any] = {"tenant_id": "tnt_1", "user_id": "usr_1"}
    defaults.update(kwargs)
    return Principal(**defaults)


def _client(settings, respond) -> TestClient:
    registry = ProviderRegistry()
    registry.register(FakeProvider(respond))
    platform = Platform.build(settings, provider_registry=registry, default_model="fake/model")
    app = create_app(platform, settings=settings)
    return TestClient(app)


# --- Profile model ----------------------------------------------------------


def test_default_specialist_is_general_with_full_scope():
    assert DEFAULT_SPECIALIST.id == GENERAL
    # Empty scope is the explicit sentinel for "the whole registry".
    assert DEFAULT_SPECIALIST.advertised_tools() == frozenset()
    assert DEFAULT_SPECIALIST.allows("anything_at_all")


def test_scoped_specialist_allows_only_declared_tools():
    specialist = Specialist(
        id="scoped",
        name_ar="محدود",
        name_en="Scoped",
        description_ar="",
        description_en="",
        tool_scope=frozenset({"read_file", "echo"}),
        extra_tools=frozenset({"finish"}),
    )
    assert specialist.allows("read_file")
    assert specialist.allows("finish")  # extra tools are always in scope
    assert not specialist.allows("run_command")
    assert specialist.advertised_tools() == frozenset({"read_file", "echo", "finish"})


def test_scoped_names_intersects_with_runtime_tools():
    specialist = Specialist(
        id="scoped",
        name_ar="محدود",
        name_en="Scoped",
        description_ar="",
        description_en="",
        tool_scope=frozenset({"read_file", "does_not_exist"}),
    )
    assert specialist.scoped_names(frozenset({"read_file", "echo"})) == frozenset({"read_file"})


# --- Catalogue --------------------------------------------------------------


def test_catalogue_has_every_builtin_and_resolves_default():
    catalogue = default_catalogue()
    assert set(catalogue.ids()) == set(BUILTIN_SPECIALISTS)
    assert catalogue.get(None).id == GENERAL
    assert catalogue.get("").id == GENERAL
    assert catalogue.get(CODING).id == CODING


def test_catalogue_unknown_id_raises_not_found():
    catalogue = default_catalogue()
    try:
        catalogue.get("nope")
    except NotFoundError as exc:
        assert "nope" in exc.message
    else:  # pragma: no cover - assertion guard
        raise AssertionError("expected NotFoundError")


def test_readiness_reports_degraded_with_missing_tools():
    catalogue = default_catalogue()
    # An empty runtime: the general agent is still 'available' (no declared
    # scope), but a scoped specialist whose tools are absent is degraded.
    report = {row["id"]: row for row in catalogue.readiness(frozenset())}
    assert report[GENERAL]["status"] == "available"
    assert report[VIDEO]["status"] in {"degraded", "unavailable"}
    assert report[VIDEO]["missing_tools"]


def test_readiness_available_when_all_scope_tools_present():
    catalogue = default_catalogue()
    coding = catalogue.get(CODING)
    present = coding.advertised_tools()
    report = {row["id"]: row for row in catalogue.readiness(frozenset(present))}
    assert report[CODING]["status"] == "available"
    assert report[CODING]["missing_tools"] == []


# --- Runner wiring ----------------------------------------------------------


async def test_runner_scopes_advertised_tools_to_specialist(make_platform, tmp_path):
    seen: list[Any] = []
    base = scripted_responder(
        plan=plan_json(("echo", "echo")),
        tool_calls=[("echo", {"text": "hi"}), ("finish", {"result": "ok"})],
        verification=verify_json(True, "ok"),
    )

    def responder(request, n):
        seen.append(request)
        return base(request, n)

    platform = make_platform(responder)
    coding = platform.specialists.get(CODING)
    runner = platform.build_runner(specialist=coding)
    result = await runner.run(
        principal=_principal(), goal="echo", workspace_dir=str(tmp_path / "ws")
    )
    assert result.status is RunStatus.COMPLETED

    # The coding scope does not include run_command; it must never be advertised
    # to the model on any call. The planner runs without tools; step calls carry
    # the advertised set, which always includes the core tools in scope.
    step_calls = [request for request in seen if request.tools]
    assert step_calls, "expected at least one step model call with tools"
    for request in seen:
        names = {spec.name for spec in request.tools}
        assert "run_command" not in names
    assert all("echo" in {spec.name for spec in request.tools} for request in step_calls)


async def test_general_runner_advertises_the_whole_registry(make_platform, tmp_path):
    seen: list[Any] = []
    base = scripted_responder(
        plan=plan_json(("echo", "echo")),
        tool_calls=[("echo", {"text": "hi"}), ("finish", {"result": "ok"})],
        verification=verify_json(True, "ok"),
    )

    def responder(request, n):
        seen.append(request)
        return base(request, n)

    platform = make_platform(responder)
    result = await platform.build_runner().run(
        principal=_principal(), goal="echo", workspace_dir=str(tmp_path / "ws")
    )
    assert result.status is RunStatus.COMPLETED
    all_names = set(platform.tools.names())
    # The planner call carries the (ranked) registry; every advertised name must
    # be a real registered tool.
    advertised = {spec.name for request in seen for spec in request.tools}
    assert advertised <= all_names
    assert "run_command" in advertised


async def test_executor_denies_a_tool_outside_the_specialist_scope(make_platform, tmp_path):
    """A model that names an out-of-scope tool is refused, not executed."""
    platform = make_platform(
        scripted_responder(
            plan=plan_json(("echo", "echo")),
            # The coding specialist never advertises run_command; the fake model
            # names it anyway to prove the executor guard refuses it.
            tool_calls=[("run_command", {"command": "echo hi"}), ("finish", {"result": "ok"})],
            verification=verify_json(True, "ok"),
        )
    )
    coding = platform.specialists.get(CODING)
    runner = platform.build_runner(specialist=coding)
    result = await runner.run(
        principal=_principal(),
        goal="echo",
        workspace_dir=str(tmp_path / "ws"),
        run_id="run_scope_1",
    )

    events = await platform.audit.list_for_run(tenant_id="tnt_1", run_id=result.run_id)
    denied = [event for event in events if event.type.value == "specialist.scope.denied"]
    assert denied, "expected a specialist.scope.denied audit event"


# --- API surface ------------------------------------------------------------


def test_specialists_endpoint_lists_profiles(settings):
    responder = scripted_responder(plan=plan_json(("echo", "echo")), tool_calls=[])
    with _client(settings, responder) as client:
        response = client.get("/specialists")
    assert response.status_code == 200
    body = {row["id"]: row for row in response.json()}
    assert set(body) == set(BUILTIN_SPECIALISTS)
    assert body[GENERAL]["status"] == "available"
    assert body[CODING]["name_en"] == "Coding Agent"


def test_specialist_detail_endpoint(settings):
    responder = scripted_responder(plan=plan_json(("echo", "echo")), tool_calls=[])
    with _client(settings, responder) as client:
        response = client.get("/specialists/education")
    assert response.status_code == 200
    assert response.json()["id"] == "education"


def test_specialist_detail_unknown_returns_404(settings):
    responder = scripted_responder(plan=plan_json(("echo", "echo")), tool_calls=[])
    with _client(settings, responder) as client:
        response = client.get("/specialists/does-not-exist")
    assert response.status_code == 404


def test_run_records_the_specialist_and_rejects_unknown(settings):
    responder = scripted_responder(
        plan=plan_json(("echo", "echo")),
        tool_calls=[("echo", {"text": "hi"}), ("finish", {"result": "تم"})],
        verification=verify_json(True, "تم"),
    )
    with _client(settings, responder) as client:
        response = client.post("/runs", json={"goal": "قل مرحبا", "specialist": "coding"})
        assert response.status_code == 200
        assert response.json()["specialist"] == "coding"

        missing = client.post("/runs", json={"goal": "قل مرحبا", "specialist": "nope"})
        assert missing.status_code == 404


def test_run_list_summary_carries_the_specialist(settings):
    responder = scripted_responder(
        plan=plan_json(("echo", "echo")),
        tool_calls=[("echo", {"text": "hi"}), ("finish", {"result": "تم"})],
        verification=verify_json(True, "تم"),
    )
    with _client(settings, responder) as client:
        client.post("/runs", json={"goal": "قل مرحبا", "specialist": "voice"})
        response = client.get("/runs")
    assert response.status_code == 200
    assert response.json()[0]["specialist"] == "voice"
