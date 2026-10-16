"""Computer-use unit tests (Phase 7).

These exercise the *real* perception, grounding, policy, budget, runtime, tools
and audit against a scripted backend (only the OS is doubled). They cover the
coordinate model, DPI transforms, Arabic-aware grounding, the observe→act→observe
discipline, per-run budgets, stop/cancellation, the screen-data boundary, and the
fact that computer tools are permission-gated and audited.
"""

from __future__ import annotations

import time

import pytest

from arabic_agent.audit.base import AuditEventType, AuditLog
from arabic_agent.audit.memory_sink import InMemoryAuditSink
from arabic_agent.computer.base import WindowInfo
from arabic_agent.computer.coords import (
    CoordinateSpace,
    MonitorInfo,
    Point,
    Rect,
    clamp_point,
    monitor_for_point,
    virtual_bounds,
)
from arabic_agent.computer.grounding import (
    GroundingTarget,
    normalize_text,
    resolve_target,
)
from arabic_agent.computer.observations import ObservationStore
from arabic_agent.computer.ocr import OcrWord
from arabic_agent.computer.perception import (
    Observation,
    PerceivedElement,
    PerceptionLayer,
    TextBlock,
)
from arabic_agent.computer.policy import ActionBudget, ComputerPolicy
from arabic_agent.computer.runtime import ComputerRuntime
from arabic_agent.computer.screen_boundary import ScreenDataBoundary, ScreenDataPolicy
from arabic_agent.core.enums import OperatingMode
from arabic_agent.core.errors import (
    ConfigurationError,
    NotFoundError,
    PermissionDeniedError,
    ResourceExhaustedError,
    ValidationError,
)

from .fake_computer import FakeComputerBackend, FakeOcr, make_pattern_image

# --- Coordinate model & DPI transforms -------------------------------------


def test_monitor_contains_and_center():
    monitor = MonitorInfo(id=0, width=1920, height=1080)
    assert monitor.contains(Point(x=10, y=10))
    assert not monitor.contains(Point(x=1920, y=10))
    assert Rect(x=0, y=0, width=100, height=50).center.as_tuple() == (50, 25)


def test_dpi_transform_round_trips():
    monitor = MonitorInfo(id=0, width=1280, height=800, scale=1.5)
    logical = Point(x=100, y=200, space=CoordinateSpace.LOGICAL)
    physical = logical.to(CoordinateSpace.PHYSICAL, monitor)
    assert physical.space is CoordinateSpace.PHYSICAL
    assert physical.x == round(100 * 1.5)
    back = physical.to(CoordinateSpace.LOGICAL, monitor)
    assert back.x == 100 and back.y == 200


def test_multi_monitor_virtual_bounds_with_negative_origin():
    left = MonitorInfo(id=0, x=-1920, y=0, width=1920, height=1080)
    primary = MonitorInfo(id=1, x=0, y=0, width=1920, height=1080, primary=True)
    bounds = virtual_bounds([left, primary])
    assert bounds.x == -1920 and bounds.width == 3840
    assert monitor_for_point([left, primary], Point(x=-10, y=10)) is left


def test_clamp_point_pulls_offscreen_point_in():
    monitors = [MonitorInfo(id=0, width=100, height=100)]
    clamped = clamp_point(monitors, Point(x=500, y=-500))
    assert (clamped.x, clamped.y) == (99, 0)


# --- Arabic-aware grounding ------------------------------------------------


def test_normalize_text_strips_diacritics_and_folds_presentation_forms():
    # "الإعدادات" with harakat/tatweel must normalise to the same key as plain.
    assert normalize_text("الإِعْدَادَات") == normalize_text("الإعدادات")
    assert normalize_text("  متعدد   الفراغات ") == "متعدد الفراغات"


def _observation(*, texts=(), elements=()) -> Observation:
    return Observation(
        id="obs_1",
        run_id="run_1",
        captured_at=0.0,
        metadata=_metadata(),
        text_blocks=tuple(
            TextBlock(text=t, rect=Rect(x=i * 100, y=0, width=90, height=20), confidence=90.0)
            for i, t in enumerate(texts)
        ),
        elements=tuple(elements),
    )


def _metadata():
    from arabic_agent.computer.base import ScreenMetadata

    monitor = MonitorInfo(id=0, width=1280, height=900)
    return ScreenMetadata(monitors=(monitor,), virtual_bounds=virtual_bounds([monitor]))


def test_grounding_matches_arabic_text_with_diacritics():
    observation = _observation(texts=["مرحبا بك", "الإعدادات"])
    target = GroundingTarget.by_text("الإعدادات")
    resolved = resolve_target(target, observation)
    assert resolved.matched == "الإعدادات"
    assert resolved.point.x > 0


def test_grounding_prefers_longest_substring_match():
    observation = _observation(texts=["حساب", "حسابي الجديد"])
    resolved = resolve_target(GroundingTarget.by_text("حساب"), observation)
    assert resolved.matched == "حسابي الجديد"


def test_grounding_raises_not_found_for_absent_text():
    observation = _observation(texts=["x"])
    with pytest.raises(NotFoundError):
        resolve_target(GroundingTarget.by_text("لا يوجد"), observation)


def test_grounding_by_element_role_and_label():
    element = PerceivedElement(
        id="e1",
        role="button",
        label="إرسال",
        rect=Rect(x=10, y=20, width=80, height=30),
    )
    observation = _observation(elements=[element])
    resolved = resolve_target(GroundingTarget.by_element(role="button", label="إرسال"), observation)
    assert resolved.point.as_tuple() == (50, 35)
    assert resolved.source == "accessibility"


def test_grounding_rejects_point_target_without_point():
    with pytest.raises(ValidationError):
        resolve_target(GroundingTarget(kind="point"), _observation())


# --- Perception fusion ------------------------------------------------------


async def test_perception_fuses_sources_and_reports_them():
    backend = FakeComputerBackend(
        windows=[WindowInfo(id="1", title="Openbox", active=True, width=100, height=100)]
    )
    ocr = FakeOcr([OcrWord(text="مرحبا", rect=Rect(x=1, y=2, width=3, height=4), confidence=95.0)])
    layer = PerceptionLayer(backend, ocr=ocr)
    observation = await layer.observe(run_id="run_1")
    assert "screenshot" in observation.sources
    assert "ocr" in observation.sources
    assert "windows" in observation.sources
    assert observation.active_window is not None
    assert observation.text_blocks[0].text == "مرحبا"


async def test_perception_reports_ocr_unavailable_honestly():
    backend = FakeComputerBackend()
    layer = PerceptionLayer(backend)  # no OCR engine
    observation = await layer.observe(run_id="run_1")
    assert "ocr" not in observation.sources
    assert any("ocr unavailable" in note for note in observation.notes)


# --- Policy & budget --------------------------------------------------------


def test_policy_clamps_out_of_bounds_point():
    policy = ComputerPolicy()
    monitors = [MonitorInfo(id=0, width=100, height=100)]
    assert policy.normalize_point(monitors, Point(x=999, y=999)).as_tuple() == (99, 99)


def test_policy_refuses_out_of_bounds_when_clamping_disabled():
    policy = ComputerPolicy(clamp_out_of_bounds=False)
    monitors = [MonitorInfo(id=0, width=100, height=100)]
    with pytest.raises(ValidationError):
        policy.normalize_point(monitors, Point(x=999, y=999))


def test_policy_rejects_disallowed_key():
    with pytest.raises(PermissionDeniedError):
        ComputerPolicy().check_keys(["f13"])


def test_policy_rejects_oversized_scroll():
    with pytest.raises(ValidationError):
        ComputerPolicy(max_scroll=10).check_scroll(100)


def test_budget_enforces_per_kind_caps():
    budget = ActionBudget(ComputerPolicy(max_pointer_actions=2))
    budget.consume("pointer", now=0.0)
    budget.consume("pointer", now=1.0)
    with pytest.raises(ResourceExhaustedError):
        budget.consume("pointer", now=2.0)


def test_budget_enforces_min_interval():
    budget = ActionBudget(ComputerPolicy(min_action_interval_seconds=1.0))
    budget.consume("pointer", now=0.0)
    with pytest.raises(ResourceExhaustedError):
        budget.consume("pointer", now=0.5)


# --- Screen-data boundary ---------------------------------------------------


def test_boundary_local_only_forbids_remote():
    boundary = ScreenDataBoundary(policy=ScreenDataPolicy.LOCAL_ONLY, mode=OperatingMode.ONLINE)
    decision = boundary.evaluate(remote=True)
    assert decision.allowed is False
    assert boundary.evaluate(remote=False).allowed is True


def test_boundary_cloud_allowed_permits_remote_when_online():
    boundary = ScreenDataBoundary(policy=ScreenDataPolicy.CLOUD_ALLOWED, mode=OperatingMode.ONLINE)
    assert boundary.evaluate(remote=True).allowed is True


def test_boundary_cloud_allowed_still_refused_offline():
    boundary = ScreenDataBoundary(policy=ScreenDataPolicy.CLOUD_ALLOWED, mode=OperatingMode.OFFLINE)
    assert boundary.evaluate(remote=True).allowed is False


def test_boundary_never_store_is_most_restrictive():
    boundary = ScreenDataBoundary(policy=ScreenDataPolicy.NEVER_STORE, mode=OperatingMode.ONLINE)
    assert boundary.evaluate(remote=True).allowed is False
    assert boundary.evaluate(remote=True).may_persist is False


# --- Runtime: observe → act → observe ---------------------------------------


def _runtime(**policy_kwargs) -> tuple[ComputerRuntime, FakeComputerBackend, InMemoryAuditSink]:
    backend = FakeComputerBackend()
    sink = InMemoryAuditSink()
    audit = AuditLog([sink])
    runtime = ComputerRuntime(
        backend=backend,
        perception=PerceptionLayer(backend, ocr=FakeOcr()),
        policy=ComputerPolicy(**policy_kwargs),
        audit=audit,
        boundary=ScreenDataBoundary(policy=ScreenDataPolicy.LOCAL_ONLY),
    )
    return runtime, backend, sink


async def test_action_without_observation_is_refused():
    runtime, _, _ = _runtime()
    with pytest.raises(ValidationError):
        await runtime.pointer_move(
            tenant_id="t", user_id="u", run_id="r", target=GroundingTarget.at_point(1, 2)
        )


async def test_observe_then_click_uses_resolved_point_and_audits():
    runtime, backend, sink = _runtime()
    observation = await runtime.observe(tenant_id="t", user_id="u", run_id="r")
    await runtime.pointer_click(
        tenant_id="t",
        user_id="u",
        run_id="r",
        target=GroundingTarget.at_point(30, 40),
        observation_id=observation.id,
    )
    assert ("pointer_click", ((30, 40), "left", False)) in backend.calls
    events = await sink.list_for_run(tenant_id="t", run_id="r")
    kinds = {e.type for e in events}
    assert AuditEventType.COMPUTER_SCREEN_CAPTURED in kinds
    assert AuditEventType.COMPUTER_CLICKED in kinds


async def test_typed_text_is_audited_by_length_only():
    runtime, _, sink = _runtime()
    await runtime.observe(tenant_id="t", user_id="u", run_id="r")
    await runtime.keyboard_type(tenant_id="t", user_id="u", run_id="r", text="كلمة السر 123")
    events = await sink.list_for_run(tenant_id="t", run_id="r")
    typed = [e for e in events if e.type is AuditEventType.COMPUTER_TEXT_TYPED]
    assert typed and typed[0].data.get("length") == len("كلمة السر 123")
    assert "كلمة" not in str(typed[0].data)


async def test_stop_halts_further_input():
    runtime, backend, _ = _runtime()
    await runtime.observe(tenant_id="t", user_id="u", run_id="r")
    runtime.request_stop("r")
    with pytest.raises(PermissionDeniedError):
        await runtime.pointer_move(
            tenant_id="t", user_id="u", run_id="r", target=GroundingTarget.at_point(1, 1)
        )
    assert backend.calls == []


async def test_capture_budget_is_enforced():
    runtime, _, _ = _runtime(max_captures=1)
    await runtime.observe(tenant_id="t", user_id="u", run_id="r")
    with pytest.raises(ResourceExhaustedError):
        await runtime.observe(tenant_id="t", user_id="u", run_id="r")


async def test_stale_observation_is_refused():
    # Build the runtime around a store that hands back a genuinely aged
    # observation: Windows' monotonic clock is coarse (~15.6 ms), so a short
    # sleep can leave the age at 0 and the freshness gate would never trip.
    real_observation = Observation(
        id="obs_stale",
        run_id="r",
        captured_at=time.monotonic() - 60.0,
        metadata=_metadata(),
    )
    store = ObservationStore()
    store.put(real_observation)
    backend = FakeComputerBackend()
    runtime = ComputerRuntime(
        backend=backend,
        perception=PerceptionLayer(backend, ocr=FakeOcr()),
        policy=ComputerPolicy(max_observation_age_seconds=0.0001),
        audit=AuditLog([InMemoryAuditSink()]),
        boundary=ScreenDataBoundary(policy=ScreenDataPolicy.LOCAL_ONLY),
        observations=store,
    )
    with pytest.raises(ValidationError):
        await runtime.pointer_move(
            tenant_id="t",
            user_id="u",
            run_id="r",
            target=GroundingTarget.at_point(1, 1),
            observation_id="obs_stale",
        )


async def test_changed_since_detects_a_real_change():
    runtime, backend, _ = _runtime()
    first = await runtime.observe(tenant_id="t", user_id="u", run_id="r")
    backend.mutate(make_pattern_image(1280, 900))
    _, changed = await runtime.changed_since(first.id, tenant_id="t", user_id="u", run_id="r")
    assert changed is True


async def test_changed_since_reports_no_change_for_identical_screen():
    runtime, _, _ = _runtime()
    first = await runtime.observe(tenant_id="t", user_id="u", run_id="r")
    _, changed = await runtime.changed_since(first.id, tenant_id="t", user_id="u", run_id="r")
    assert changed is False


async def test_unavailable_backend_reports_honestly():
    from arabic_agent.computer.backends.null import NullComputerBackend

    backend = NullComputerBackend("no display")
    sink = InMemoryAuditSink()
    runtime = ComputerRuntime(
        backend=backend,
        perception=PerceptionLayer(backend),
        policy=ComputerPolicy(),
        audit=AuditLog([sink]),
        boundary=ScreenDataBoundary(),
    )
    with pytest.raises(ConfigurationError):
        await runtime.observe(tenant_id="t", user_id="u", run_id="r")


# --- OCR engine construction & attachment provider --------------------------


def test_build_ocr_engine_defaults_to_arabic_first():
    from arabic_agent.computer.ocr import NullOcr, TesseractOcr, build_ocr_engine

    engine = build_ocr_engine(languages=("ara", "eng"))
    assert isinstance(engine, (TesseractOcr, NullOcr))
    # If pytesseract is present the languages are threaded through; otherwise the
    # engine reports itself unavailable rather than pretending to recognise.
    if isinstance(engine, TesseractOcr):
        assert engine.languages == ("ara", "eng")
    else:
        assert engine.available is False


async def test_observation_attachment_provider_returns_image_local():
    from arabic_agent.computer.attachments import ObservationAttachmentProvider

    runtime, _, _ = _runtime()
    observation = await runtime.observe(tenant_id="t", user_id="u", run_id="r")
    provider = ObservationAttachmentProvider(
        runtime=runtime,
        boundary=runtime.boundary,
        destination_is_remote=False,
    )
    attachments = await provider.attachments_for(run_id="r")
    assert len(attachments) == 1
    assert attachments[0].image.mime_type == "image/png"
    assert attachments[0].image.width == observation.image_width
    assert attachments[0].description.startswith("screen capture")


async def test_observation_attachment_provider_refuses_remote_under_local_only():
    from arabic_agent.computer.attachments import ObservationAttachmentProvider

    runtime, _, _ = _runtime()
    await runtime.observe(tenant_id="t", user_id="u", run_id="r")
    provider = ObservationAttachmentProvider(
        runtime=runtime,
        boundary=runtime.boundary,
        destination_is_remote=True,
    )
    assert await provider.attachments_for(run_id="r") == []


async def test_observation_attachment_provider_empty_without_observation():
    from arabic_agent.computer.attachments import ObservationAttachmentProvider

    runtime, _, _ = _runtime()
    provider = ObservationAttachmentProvider(
        runtime=runtime,
        boundary=runtime.boundary,
        destination_is_remote=False,
    )
    assert await provider.attachments_for(run_id="never-observed") == []


async def test_observation_attachment_provider_allows_remote_when_cloud_allowed():
    from arabic_agent.computer.attachments import ObservationAttachmentProvider

    backend = FakeComputerBackend()
    runtime = ComputerRuntime(
        backend=backend,
        perception=PerceptionLayer(backend, ocr=FakeOcr()),
        policy=ComputerPolicy(),
        audit=AuditLog([InMemoryAuditSink()]),
        boundary=ScreenDataBoundary(
            policy=ScreenDataPolicy.CLOUD_ALLOWED, mode=OperatingMode.ONLINE
        ),
    )
    await runtime.observe(tenant_id="t", user_id="u", run_id="r")
    provider = ObservationAttachmentProvider(
        runtime=runtime,
        boundary=runtime.boundary,
        destination_is_remote=True,
    )
    assert len(await provider.attachments_for(run_id="r")) == 1
