"""Real X11 computer-control end-to-end test (Phase 7).

Unlike the unit suite, nothing here is doubled: the *real* X11 backend captures
the actual display and synthesises real input through ``xdotool``, driven by the
real runtime, policy and tools. It is opt-in via ``AAP_TEST_DISPLAY`` so a
headless CI without an X server is unaffected; when a display is provided (the
project's dev container runs ``Xvfb :99`` with ``openbox``), it exercises the
full path: capture → perceive → ground → act → observe → verify.

The display is taken from the test environment, never from model input.
"""

from __future__ import annotations

import os

import pytest

from arabic_agent.audit.base import AuditLog
from arabic_agent.audit.memory_sink import InMemoryAuditSink
from arabic_agent.computer.backends.linux_x11 import X11Backend
from arabic_agent.computer.coords import CoordinateSpace
from arabic_agent.computer.grounding import GroundingTarget
from arabic_agent.computer.perception import PerceptionLayer
from arabic_agent.computer.policy import ComputerPolicy
from arabic_agent.computer.runtime import ComputerRuntime
from arabic_agent.computer.screen_boundary import ScreenDataBoundary, ScreenDataPolicy
from arabic_agent.core.errors import ConfigurationError, NotFoundError

TEST_DISPLAY = os.environ.get("AAP_TEST_DISPLAY", "")

pytestmark = pytest.mark.skipif(
    not TEST_DISPLAY, reason="AAP_TEST_DISPLAY not set (no real X display available)"
)


@pytest.fixture()
def backend() -> X11Backend:
    return X11Backend(display=TEST_DISPLAY)


def _runtime(backend: X11Backend) -> ComputerRuntime:
    return ComputerRuntime(
        backend=backend,
        perception=PerceptionLayer(backend),
        policy=ComputerPolicy(),
        audit=AuditLog([InMemoryAuditSink()]),
        boundary=ScreenDataBoundary(policy=ScreenDataPolicy.LOCAL_ONLY),
    )


async def test_backend_is_available_and_reports_a_monitor(backend):
    assert backend.available, backend.unavailable_reason
    metadata = await backend.get_screen_metadata()
    assert metadata.monitors
    assert metadata.monitors[0].width > 0 and metadata.monitors[0].height > 0
    assert metadata.virtual_bounds is not None


async def test_real_capture_returns_a_png(backend):
    captured = await backend.capture_screen()
    assert captured.width > 0 and captured.height > 0
    assert captured.data[:8] == b"\x89PNG\r\n\x1a\n"
    assert captured.space is CoordinateSpace.PHYSICAL


async def test_real_observe_ground_and_click(backend):
    runtime = _runtime(backend)
    observation = await runtime.observe(tenant_id="t", user_id="u", run_id="run_e2e")
    assert observation.image is not None
    assert "screenshot" in observation.sources

    # Act on a concrete on-screen point derived from the real monitor geometry.
    monitor = observation.metadata.monitors[0]
    point = GroundingTarget.at_point(monitor.width // 4, monitor.height // 4)
    resolved = await runtime.pointer_move(
        tenant_id="t",
        user_id="u",
        run_id="run_e2e",
        target=point,
        observation_id=observation.id,
    )
    assert resolved.point.as_tuple() == (monitor.width // 4, monitor.height // 4)

    # The real pointer actually moved.
    cursor = await backend.get_cursor_position()
    assert cursor is not None
    assert cursor.as_tuple() == (monitor.width // 4, monitor.height // 4)


async def test_real_keyboard_types_arabic(backend):
    runtime = _runtime(backend)
    observation = await runtime.observe(tenant_id="t", user_id="u", run_id="run_kb")
    length = await runtime.keyboard_type(
        tenant_id="t", user_id="u", run_id="run_kb", text="مرحبا", observation_id=observation.id
    )
    assert length == len("مرحبا")


async def test_real_windows_are_listed(backend):
    runtime = _runtime(backend)
    windows = await runtime.list_windows(run_id="run_win")
    # openbox maps at least one window on this display.
    assert isinstance(windows, list)


async def test_verification_detects_a_real_screen_change(backend):
    runtime = _runtime(backend)
    first = await runtime.observe(tenant_id="t", user_id="u", run_id="run_verify")
    # Map a real window so the screen genuinely changes, then verify.
    import asyncio

    proc = await asyncio.create_subprocess_exec(
        "xmessage",
        "-display",
        TEST_DISPLAY,
        "مرحبا",
        env={**os.environ, "DISPLAY": TEST_DISPLAY},
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        await asyncio.sleep(1.0)
        _, changed = await runtime.changed_since(
            first.id, tenant_id="t", user_id="u", run_id="run_verify"
        )
    finally:
        proc.terminate()
        await proc.wait()
    assert changed is True


async def test_grounding_miss_raises_not_found(backend):
    runtime = _runtime(backend)
    observation = await runtime.observe(tenant_id="t", user_id="u", run_id="run_miss")
    with pytest.raises(NotFoundError):
        await runtime.pointer_move(
            tenant_id="t",
            user_id="u",
            run_id="run_miss",
            target=GroundingTarget.by_text("نص-غير-موجود-إطلاقا"),
            observation_id=observation.id,
        )


async def test_unavailable_display_reports_honestly():
    backend = X11Backend(display=":99999")
    assert backend.available is False
    with pytest.raises(ConfigurationError):
        await backend.capture_screen()
