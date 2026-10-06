"""Real Playwright/Chromium browser end-to-end test (Phase 8).

Unlike the unit suite, nothing here is doubled: the *real* Playwright backend
drives a real Chromium against a real loopback HTTP server, through the real
runtime, policy, observation store, data boundary and audit log. It is opt-in
via ``AAP_TEST_BROWSER=1`` so a host without a Chromium binary is unaffected;
when enabled it exercises the full path: navigate -> observe -> interact ->
download -> dialog -> verify.

The target server binds to loopback and is taken from the test fixture, never
from model input; the URL policy is told to allow local addresses for the test.
"""

from __future__ import annotations

import http.server
import os
import threading
from functools import partial
from pathlib import Path

import pytest

from arabic_agent.audit.base import AuditEventType, AuditLog
from arabic_agent.audit.memory_sink import InMemoryAuditSink
from arabic_agent.browser.backends import build_browser_backend
from arabic_agent.browser.boundary import WebContentPolicy, WebDataBoundary
from arabic_agent.browser.policy import BrowserPolicy
from arabic_agent.browser.runtime import BrowserRuntime
from arabic_agent.browser.url_security import UrlPolicy
from arabic_agent.core.enums import OperatingMode

TEST_BROWSER = os.environ.get("AAP_TEST_BROWSER", "")

pytestmark = [
    pytest.mark.browser,
    pytest.mark.skipif(not TEST_BROWSER, reason="AAP_TEST_BROWSER not set (no real browser e2e)"),
]

_HOME = """<!doctype html>
<html lang="ar" dir="rtl"><head><meta charset="utf-8"><title>الصفحة الرئيسية</title></head>
<body>
  <h1>مرحبا</h1>
  <form id="f" action="/thanks.html" method="get">
    <label for="name">الاسم</label>
    <input id="name" name="name" placeholder="اكتب اسمك">
    <button type="submit">إرسال</button>
  </form>
  <a href="/second">الصفحة الثانية</a>
  <button id="dl" onclick="location.href='/data.bin'">تنزيل</button>
  <button id="dlg" onclick="confirm('Proceed?')">تأكيد</button>
</body></html>
"""

_SECOND = """<!doctype html>
<html><head><meta charset="utf-8"><title>Second</title></head>
<body><h1>Second page</h1><p id="msg">unchanged</p>
<button id="go" onclick="document.getElementById('msg').textContent='changed'">Change</button>
</body></html>
"""

_THANKS = """<!doctype html>
<html><head><meta charset="utf-8"><title>Thanks</title></head>
<body><h1>Thanks</h1><p>submitted</p></body></html>
"""


def _read_bytes(path: str) -> bytes:
    # A plain sync helper: called from an async test, but the blocking read is
    # trivial and belongs outside the coroutine for the lint rule's benefit.
    return Path(path).read_bytes()


@pytest.fixture()
def site(tmp_path: Path):
    root = tmp_path / "site"
    root.mkdir()
    (root / "index.html").write_text(_HOME, encoding="utf-8")
    (root / "second.html").write_text(_SECOND, encoding="utf-8")
    (root / "thanks.html").write_text(_THANKS, encoding="utf-8")
    (root / "data.bin").write_bytes(b"binary-download-payload" * 4)

    handler = partial(http.server.SimpleHTTPRequestHandler, directory=str(root))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address[:2]
    try:
        yield f"http://{host}:{port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.fixture()
def backend(tmp_path: Path):
    download_dir = tmp_path / "downloads"
    download_dir.mkdir()
    built = build_browser_backend(download_dir=str(download_dir), headless=True)
    if not built.available:
        pytest.skip(f"no real browser backend: {built.unavailable_reason}")
    return built


def _runtime(backend) -> tuple[BrowserRuntime, InMemoryAuditSink]:
    sink = InMemoryAuditSink()
    audit = AuditLog([sink])
    runtime = BrowserRuntime(
        backend=backend,
        policy=BrowserPolicy(url_policy=UrlPolicy(allow_local_addresses=True)),
        audit=audit,
        boundary=WebDataBoundary(policy=WebContentPolicy.LOCAL_ONLY, mode=OperatingMode.ONLINE),
    )
    return runtime, sink


async def _observe(runtime: BrowserRuntime, run_id: str, observation_id: str):
    return await runtime.observe(
        tenant_id="tnt_1", user_id="usr_1", run_id=run_id, observation_id=observation_id
    )


async def test_real_navigate_observe_and_screenshot(backend, site):
    runtime, sink = _runtime(backend)
    try:
        observation = await runtime.navigate(
            tenant_id="tnt_1",
            user_id="usr_1",
            run_id="run_e2e",
            url=f"{site}/index.html",
            observation_id="obs1",
        )
        assert "الصفحة الرئيسية" in observation.title
        assert observation.headings and "مرحبا" in observation.headings[0]
        # The form field and submit button are exposed as actionable refs.
        refs = {e.ref for e in observation.elements}
        assert refs
        assert observation.forms

        shot = await runtime.observe(
            tenant_id="tnt_1",
            user_id="usr_1",
            run_id="run_e2e",
            observation_id="obs2",
            include_screenshot=True,
        )
        assert shot.screenshot is not None
        assert shot.screenshot.data[:8] == b"\x89PNG\r\n\x1a\n"

        events = await sink.list_for_run(tenant_id="tnt_1", run_id="run_e2e")
        assert any(e.type is AuditEventType.BROWSER_NAVIGATED for e in events)
        assert any(e.type is AuditEventType.BROWSER_OBSERVED for e in events)
    finally:
        await runtime.aclose()


async def test_real_interaction_changes_the_page_and_is_verified(backend, site):
    runtime, _ = _runtime(backend)
    try:
        observation = await runtime.navigate(
            tenant_id="tnt_1",
            user_id="usr_1",
            run_id="run_e2e",
            url=f"{site}/second.html",
            observation_id="obs1",
        )
        button = next(
            (e for e in observation.elements if e.name == "Change" or e.text == "Change"),
            None,
        )
        assert button is not None, [e.model_dump() for e in observation.elements]

        acted = await runtime.act(
            tenant_id="tnt_1",
            user_id="usr_1",
            run_id="run_e2e",
            observation_id="obs1",
            action="click",
            ref=button.ref,
            new_observation_id="obs2",
        )
        assert "changed" in acted.text

        _fresh, changed = await runtime.changed_since(
            tenant_id="tnt_1",
            user_id="usr_1",
            run_id="run_e2e",
            observation_id="obs1",
            new_observation_id="obs3",
        )
        assert changed is True
    finally:
        await runtime.aclose()


async def test_real_form_typing_and_submit(backend, site):
    runtime, _ = _runtime(backend)
    try:
        observation = await runtime.navigate(
            tenant_id="tnt_1",
            user_id="usr_1",
            run_id="run_e2e",
            url=f"{site}/index.html",
            observation_id="obs1",
        )
        field = next((e for e in observation.elements if e.name == "الاسم"), None)
        assert field is not None, [e.model_dump() for e in observation.elements]

        typed = await runtime.act(
            tenant_id="tnt_1",
            user_id="usr_1",
            run_id="run_e2e",
            observation_id="obs1",
            action="type",
            ref=field.ref,
            text="محمد",
            new_observation_id="obs2",
        )
        # The typed value round-trips through the form field.
        assert any(e.value == "محمد" for e in typed.forms[0].fields)

        submit = next(
            (e for e in typed.elements if e.role == "button" and e.name == "إرسال"),
            None,
        )
        assert submit is not None
        # A submit is refused without the trusted approval signal.
        from arabic_agent.core.errors import PermissionDeniedError

        with pytest.raises(PermissionDeniedError):
            await runtime.act(
                tenant_id="tnt_1",
                user_id="usr_1",
                run_id="run_e2e",
                observation_id="obs2",
                action="submit",
                ref=submit.ref,
            )
        submitted = await runtime.act(
            tenant_id="tnt_1",
            user_id="usr_1",
            run_id="run_e2e",
            observation_id="obs2",
            action="submit",
            ref=submit.ref,
            approved_submit=True,
            new_observation_id="obs3",
        )
        assert "Thanks" in submitted.title or "submitted" in submitted.text
    finally:
        await runtime.aclose()


async def test_real_download_is_confined_and_dialog_is_not_auto_accepted(backend, site, tmp_path):
    runtime, _ = _runtime(backend)
    try:
        observation = await runtime.navigate(
            tenant_id="tnt_1",
            user_id="usr_1",
            run_id="run_e2e",
            url=f"{site}/index.html",
            observation_id="obs1",
        )
        download_ref = next(
            (e for e in observation.elements if e.name == "تنزيل" or e.text == "تنزيل"), None
        )
        assert download_ref is not None
        file, _fresh = await runtime.download(
            tenant_id="tnt_1",
            user_id="usr_1",
            run_id="run_e2e",
            observation_id="obs1",
            ref=download_ref.ref,
        )
        assert file.size_bytes > 0
        assert Path(file.path).parent == (tmp_path / "downloads")
        assert _read_bytes(file.path).startswith(b"binary-download-payload")

        dialog_ref = next(
            (e for e in observation.elements if e.name == "تأكيد" or e.text == "تأكيد"), None
        )
        assert dialog_ref is not None
        await runtime.act(
            tenant_id="tnt_1",
            user_id="usr_1",
            run_id="run_e2e",
            observation_id="obs1",
            action="click",
            ref=dialog_ref.ref,
        )
        dialog = await runtime.dialog(tenant_id="tnt_1", user_id="usr_1", run_id="run_e2e")
        assert dialog is not None and dialog.type == "confirm"
        # Dismissing is not consequential and needs no approval.
        resolved = await runtime.resolve_dialog(
            tenant_id="tnt_1",
            user_id="usr_1",
            run_id="run_e2e",
            observation_id="obs1",
            accept=False,
        )
        assert resolved.dialog is None
    finally:
        await runtime.aclose()
