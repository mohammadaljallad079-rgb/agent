"""Browser runtime unit tests (Phase 8).

These run the *real* runtime -- URL policy, action budget, observation store,
web-content boundary, source ledger and audit -- against a scripted browser
backend (only Chromium is doubled). They cover the observe -> act -> observe
discipline, freshness, the submit/approval backstop, budgets, stop/cancellation,
downloads/uploads, dialogs, change verification and the audit trail, plus the
fact that typed text is never written to the audit log.
"""

from __future__ import annotations

import time

import pytest

from arabic_agent.audit.base import AuditEventType, AuditLog
from arabic_agent.audit.memory_sink import InMemoryAuditSink
from arabic_agent.browser.attachments import BrowserAttachmentProvider
from arabic_agent.browser.base import (
    DialogInfo,
    DownloadedFile,
    ElementInfo,
)
from arabic_agent.browser.boundary import WebContentPolicy, WebDataBoundary
from arabic_agent.browser.observations import ObservationStore, build_observation
from arabic_agent.browser.policy import BrowserPolicy
from arabic_agent.browser.runtime import BrowserRuntime
from arabic_agent.browser.url_security import UrlPolicy
from arabic_agent.core.enums import OperatingMode
from arabic_agent.core.errors import (
    NotFoundError,
    PermissionDeniedError,
    ResourceExhaustedError,
    ValidationError,
)

from .fake_browser import FakeBrowserBackend, make_snapshot

BUTTON = ElementInfo(ref="e1", role="button", name="Go")
FIELD = ElementInfo(ref="e2", role="textbox", name="Name")


def _runtime(
    *,
    backend: FakeBrowserBackend | None = None,
    policy: BrowserPolicy | None = None,
    boundary: WebDataBoundary | None = None,
    observations: ObservationStore | None = None,
) -> tuple[BrowserRuntime, InMemoryAuditSink, FakeBrowserBackend]:
    sink = InMemoryAuditSink()
    audit = AuditLog([sink])
    backend = backend or FakeBrowserBackend(
        snapshot=make_snapshot(elements=(BUTTON, FIELD), links=())
    )
    runtime = BrowserRuntime(
        backend=backend,
        policy=policy or BrowserPolicy(url_policy=UrlPolicy()),
        audit=audit,
        boundary=boundary
        or WebDataBoundary(policy=WebContentPolicy.LOCAL_ONLY, mode=OperatingMode.OFFLINE),
        observations=observations,
    )
    return runtime, sink, backend


async def _observe(runtime: BrowserRuntime, run_id: str = "run1"):
    return await runtime.observe(
        tenant_id="tnt_1",
        user_id="usr_1",
        run_id=run_id,
        observation_id="obs1",
    )


# --- observe ----------------------------------------------------------------


async def test_observe_starts_backend_records_source_and_audits():
    runtime, sink, backend = _runtime()
    observation = await _observe(runtime)
    assert backend.started
    assert observation.url == "https://example.com/"
    assert runtime.latest_observation(run_id="run1") is not None
    assert [s.url for s in runtime.sources.sources("run1")] == ["https://example.com/"]
    events = await sink.list_for_run(tenant_id="tnt_1", run_id="run1")
    assert any(e.type is AuditEventType.BROWSER_OBSERVED for e in events)


async def test_observe_with_screenshot_attaches_image_but_audits_no_bytes():
    runtime, sink, _ = _runtime()
    observation = await runtime.observe(
        tenant_id="tnt_1",
        user_id="usr_1",
        run_id="run1",
        observation_id="obs1",
        include_screenshot=True,
    )
    assert observation.screenshot is not None
    events = await sink.list_for_run(tenant_id="tnt_1", run_id="run1")
    observed = next(e for e in events if e.type is AuditEventType.BROWSER_OBSERVED)
    assert observed.data["screenshot"] is True
    # No raw image bytes anywhere in the event payload.
    assert all(not isinstance(v, bytes) for v in observed.data.values())


# --- act: observation discipline -------------------------------------------


async def test_act_without_observation_is_refused():
    runtime, _, backend = _runtime()
    with pytest.raises(ValidationError):
        await runtime.act(
            tenant_id="tnt_1",
            user_id="usr_1",
            run_id="run1",
            observation_id="",
            action="click",
            ref="e1",
        )
    assert backend.calls == []


async def test_act_on_stale_observation_is_refused():
    store = ObservationStore()
    stale = build_observation(
        observation_id="obs1",
        run_id="run1",
        snapshot=make_snapshot(elements=(BUTTON,)),
        max_text_chars=1000,
    )
    store.put(stale.model_copy(update={"captured_at": time.monotonic() - 500.0}))
    runtime, _, _ = _runtime(observations=store)
    with pytest.raises(ValidationError, match="stale"):
        await runtime.act(
            tenant_id="tnt_1",
            user_id="usr_1",
            run_id="run1",
            observation_id="obs1",
            action="click",
            ref="e1",
        )


async def test_act_with_unknown_ref_is_refused():
    runtime, _, _ = _runtime()
    await _observe(runtime)
    with pytest.raises(ValidationError):
        await runtime.act(
            tenant_id="tnt_1",
            user_id="usr_1",
            run_id="run1",
            observation_id="obs1",
            action="click",
            ref="does-not-exist",
        )


async def test_act_dispatches_each_action_and_reobserves():
    runtime, _, backend = _runtime()
    await _observe(runtime)
    for action, kwargs, expected in [
        ("click", {"ref": "e1"}, ("click", "e1")),
        ("type", {"ref": "e2", "text": "مرحبا"}, ("type_text", ("e2", "مرحبا", True))),
        ("press", {"key": "Enter"}, ("press_key", ("Enter", None))),
        ("select", {"ref": "e2", "value": "v"}, ("select_option", ("e2", "v"))),
        ("check", {"ref": "e1", "checked": True}, ("set_checked", ("e1", True))),
        ("scroll", {"amount": 200}, ("scroll", (200, None))),
    ]:
        observation = await runtime.act(
            tenant_id="tnt_1",
            user_id="usr_1",
            run_id="run1",
            observation_id="obs1",
            action=action,
            **kwargs,
        )
        assert observation.id == "obs1"
        assert expected in backend.calls
    # Every act re-observes, so snapshots outnumber actions.
    assert sum(1 for c in backend.calls if c[0] == "snapshot") == 7


async def test_act_unknown_action_is_refused():
    runtime, _, _ = _runtime()
    await _observe(runtime)
    with pytest.raises(ValidationError):
        await runtime.act(
            tenant_id="tnt_1",
            user_id="usr_1",
            run_id="run1",
            observation_id="obs1",
            action="teleport",
        )


async def test_typed_text_is_audited_as_length_only():
    runtime, sink, _ = _runtime()
    await _observe(runtime)
    secret = "super-secret-password"
    await runtime.act(
        tenant_id="tnt_1",
        user_id="usr_1",
        run_id="run1",
        observation_id="obs1",
        action="type",
        ref="e2",
        text=secret,
    )
    events = await sink.list_for_run(tenant_id="tnt_1", run_id="run1")
    interacted = next(e for e in events if e.type is AuditEventType.BROWSER_INTERACTED)
    assert interacted.data["text_length"] == len(secret)
    assert secret not in str(interacted.data)


# --- submit / approval backstop --------------------------------------------


async def test_submit_requires_approval_backstop():
    runtime, _, backend = _runtime()
    await _observe(runtime)
    with pytest.raises(PermissionDeniedError):
        await runtime.act(
            tenant_id="tnt_1",
            user_id="usr_1",
            run_id="run1",
            observation_id="obs1",
            action="submit",
            ref="e1",
        )
    # Nothing reached the backend.
    assert not any(c[0] == "click" for c in backend.calls)


async def test_approved_submit_executes_and_is_audited_distinctly():
    runtime, sink, backend = _runtime()
    await _observe(runtime)
    await runtime.act(
        tenant_id="tnt_1",
        user_id="usr_1",
        run_id="run1",
        observation_id="obs1",
        action="submit",
        ref="e1",
        approved_submit=True,
    )
    assert ("click", "e1") in backend.calls
    events = await sink.list_for_run(tenant_id="tnt_1", run_id="run1")
    assert any(e.type is AuditEventType.BROWSER_FORM_SUBMITTED for e in events)


# --- navigation -------------------------------------------------------------


async def test_navigate_validates_and_audits_risk():
    runtime, sink, backend = _runtime()
    await runtime.navigate(
        tenant_id="tnt_1",
        user_id="usr_1",
        run_id="run1",
        url="https://example.com/page",
        observation_id="obs1",
    )
    assert ("navigate", "https://example.com/page") in backend.calls
    events = await sink.list_for_run(tenant_id="tnt_1", run_id="run1")
    navigated = next(e for e in events if e.type is AuditEventType.BROWSER_NAVIGATED)
    assert navigated.data["risk"] == "safe"


async def test_navigate_blocked_url_never_reaches_backend():
    runtime, _, backend = _runtime()
    with pytest.raises(PermissionDeniedError):
        await runtime.navigate(
            tenant_id="tnt_1",
            user_id="usr_1",
            run_id="run1",
            url="http://169.254.169.254/latest/meta-data/",
            observation_id="obs1",
        )
    assert not any(c[0] == "navigate" for c in backend.calls)


async def test_open_tab_respects_max_tabs():
    policy = BrowserPolicy(url_policy=UrlPolicy(), max_tabs=1)
    runtime, _, _ = _runtime(policy=policy)
    with pytest.raises(ValidationError, match="tab limit"):
        await runtime.open_tab(
            tenant_id="tnt_1",
            user_id="usr_1",
            run_id="run1",
            url="https://example.com/",
            observation_id="obs1",
        )


async def test_go_back_forward_reload_consume_navigation_budget():
    policy = BrowserPolicy(url_policy=UrlPolicy(), max_navigations=2)
    runtime, _, backend = _runtime(policy=policy)
    await runtime.go_back(tenant_id="tnt_1", user_id="usr_1", run_id="run1", observation_id="obs1")
    await runtime.go_forward(
        tenant_id="tnt_1", user_id="usr_1", run_id="run1", observation_id="obs2"
    )
    with pytest.raises(ResourceExhaustedError):
        await runtime.reload(
            tenant_id="tnt_1", user_id="usr_1", run_id="run1", observation_id="obs3"
        )
    assert ("go_back", None) in backend.calls and ("go_forward", None) in backend.calls


# --- stop / cancellation ----------------------------------------------------


async def test_stop_halts_actions():
    runtime, _, _ = _runtime()
    runtime.request_stop("run1")
    with pytest.raises(PermissionDeniedError):
        await _observe(runtime)
    runtime.clear_stop("run1")
    observation = await _observe(runtime)
    assert observation.url


# --- files ------------------------------------------------------------------


async def test_download_requires_known_ref_and_audits():
    downloaded = DownloadedFile(
        suggested_filename="report.pdf", path="/tmp/report.pdf", size_bytes=10
    )
    backend = FakeBrowserBackend(snapshot=make_snapshot(elements=(BUTTON,)), download=downloaded)
    runtime, sink, _ = _runtime(backend=backend)
    await _observe(runtime)
    file, _fresh = await runtime.download(
        tenant_id="tnt_1",
        user_id="usr_1",
        run_id="run1",
        observation_id="obs1",
        ref="e1",
    )
    assert file.suggested_filename == "report.pdf"
    events = await sink.list_for_run(tenant_id="tnt_1", run_id="run1")
    assert any(e.type is AuditEventType.BROWSER_DOWNLOADED for e in events)


async def test_upload_audits_filename_only_not_full_path():
    runtime, sink, backend = _runtime()
    await _observe(runtime)
    await runtime.upload(
        tenant_id="tnt_1",
        user_id="usr_1",
        run_id="run1",
        observation_id="obs1",
        ref="e1",
        path="/home/user/private/secret.txt",
    )
    assert any(c[0] == "upload" for c in backend.calls)
    events = await sink.list_for_run(tenant_id="tnt_1", run_id="run1")
    uploaded = next(e for e in events if e.type is AuditEventType.BROWSER_UPLOADED)
    assert uploaded.data["filename"] == "secret.txt"
    assert "secret.txt" in str(uploaded.data)
    assert "/home/user/private" not in str(uploaded.data)


# --- dialogs ----------------------------------------------------------------


async def test_dialog_get_returns_open_dialog():
    backend = FakeBrowserBackend(dialog=DialogInfo(type="confirm", message="Sure?"))
    runtime, _, _ = _runtime(backend=backend)
    dialog = await runtime.dialog(tenant_id="tnt_1", user_id="usr_1", run_id="run1")
    assert dialog is not None and dialog.type == "confirm"


async def test_action_that_opens_a_dialog_returns_previous_observation():
    """A click that opens a dialog must not attempt a frozen DOM snapshot."""
    backend = FakeBrowserBackend(
        snapshot=make_snapshot(elements=(BUTTON,)),
        dialog=DialogInfo(type="confirm", message="Sure?"),
    )
    runtime, _, _ = _runtime(backend=backend)
    await _observe(runtime)
    observation = await runtime.act(
        tenant_id="tnt_1",
        user_id="usr_1",
        run_id="run1",
        observation_id="obs1",
        action="click",
        ref="e1",
    )
    assert observation.dialog is not None
    # No post-click snapshot was attempted while the dialog is open.
    assert sum(1 for c in backend.calls if c[0] == "snapshot") == 1


async def test_accept_dialog_requires_approval():
    backend = FakeBrowserBackend(dialog=DialogInfo(type="confirm", message="Sure?"))
    runtime, sink, _ = _runtime(backend=backend)
    with pytest.raises(PermissionDeniedError):
        await runtime.resolve_dialog(
            tenant_id="tnt_1",
            user_id="usr_1",
            run_id="run1",
            observation_id="obs1",
            accept=True,
        )
    # Dismissing is not consequential and does not need approval.
    dismissed = await runtime.resolve_dialog(
        tenant_id="tnt_1",
        user_id="usr_1",
        run_id="run1",
        observation_id="obs1",
        accept=False,
    )
    assert dismissed.dialog is None
    assert ("dismiss_dialog", None) in backend.calls
    events = await sink.list_for_run(tenant_id="tnt_1", run_id="run1")
    assert any(e.type is AuditEventType.BROWSER_DIALOG_RESOLVED for e in events)


async def test_accept_dialog_with_approval_is_audited():
    backend = FakeBrowserBackend(dialog=DialogInfo(type="confirm", message="Sure?"))
    runtime, sink, _ = _runtime(backend=backend)
    resolved = await runtime.resolve_dialog(
        tenant_id="tnt_1",
        user_id="usr_1",
        run_id="run1",
        observation_id="obs1",
        accept=True,
        approved=True,
    )
    assert resolved.dialog is None
    assert ("accept_dialog", None) in backend.calls
    events = await sink.list_for_run(tenant_id="tnt_1", run_id="run1")
    accepted = next(e for e in events if e.type is AuditEventType.BROWSER_DIALOG_RESOLVED)
    assert accepted.data["accept"] is True


# --- verification -----------------------------------------------------------


async def test_changed_since_detects_material_change():
    backend = FakeBrowserBackend(snapshot=make_snapshot(title="One", text="alpha"))
    runtime, sink, _ = _runtime(backend=backend)
    await _observe(runtime)
    backend.set_snapshot(make_snapshot(title="Two", text="beta"))
    _fresh, changed = await runtime.changed_since(
        tenant_id="tnt_1",
        user_id="usr_1",
        run_id="run1",
        observation_id="obs1",
        new_observation_id="obs2",
    )
    assert changed is True
    events = await sink.list_for_run(tenant_id="tnt_1", run_id="run1")
    assert any(e.type is AuditEventType.BROWSER_ACTION_VERIFIED for e in events)


async def test_changed_since_reports_no_change():
    backend = FakeBrowserBackend(snapshot=make_snapshot())
    runtime, _, _ = _runtime(backend=backend)
    await _observe(runtime)
    _fresh, changed = await runtime.changed_since(
        tenant_id="tnt_1",
        user_id="usr_1",
        run_id="run1",
        observation_id="obs1",
        new_observation_id="obs2",
    )
    assert changed is False


async def test_get_observation_missing_run_is_not_found():
    runtime, _, _ = _runtime()
    with pytest.raises(NotFoundError):
        runtime.get_observation("obs1", run_id="run1")


# --- research ---------------------------------------------------------------


async def test_note_source_and_source_list():
    runtime, _, _ = _runtime()
    await runtime.note_source(
        run_id="run1", url="https://www.nasa.gov/x", title="NASA", excerpt="e"
    )
    listing = runtime.source_list(run_id="run1")
    assert listing[0]["quality"] == "official"


# --- attachments / boundary -------------------------------------------------


async def test_attachment_provider_withholds_screenshot_under_local_only():
    runtime, _, _ = _runtime(
        boundary=WebDataBoundary(policy=WebContentPolicy.LOCAL_ONLY, mode=OperatingMode.ONLINE)
    )
    await runtime.observe(
        tenant_id="tnt_1",
        user_id="usr_1",
        run_id="run1",
        observation_id="obs1",
        include_screenshot=True,
    )
    provider = BrowserAttachmentProvider(
        runtime=runtime, boundary=runtime.boundary, destination_is_remote=True
    )
    assert await provider.attachments_for(run_id="run1") == []


async def test_attachment_provider_offers_screenshot_when_cloud_allowed():
    runtime, _, _ = _runtime(
        boundary=WebDataBoundary(policy=WebContentPolicy.CLOUD_ALLOWED, mode=OperatingMode.ONLINE)
    )
    await runtime.observe(
        tenant_id="tnt_1",
        user_id="usr_1",
        run_id="run1",
        observation_id="obs1",
        include_screenshot=True,
    )
    provider = BrowserAttachmentProvider(
        runtime=runtime, boundary=runtime.boundary, destination_is_remote=True
    )
    attachments = await provider.attachments_for(run_id="run1")
    assert len(attachments) == 1
    assert attachments[0].source == "browser_observe"


async def test_runtime_close_releases_backend():
    runtime, _, backend = _runtime()
    await _observe(runtime)
    await runtime.aclose()
    assert backend.closed
