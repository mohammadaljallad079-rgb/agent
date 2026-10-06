"""Playwright/Chromium browser backend.

This is the real automation seam. It drives Chromium through Playwright's async
API, but exposes only the small, page-scoped :class:`BrowserBackend` contract --
no raw Playwright object ever crosses into the tools or the model.

Design decisions that matter for safety and for a *usable* observation:

* **Semantic refs, not selectors.** Each observation walks the interactive
  elements, tags the visible ones with a stable ``data-aap-ref`` and reports the
  element's role, accessible name, label, value and bounding box. The model acts
  on a ``ref`` and never writes a CSS selector or XPath.
* **Bounded extraction.** The DOM walk is capped (elements, links, forms, text);
  the raw DOM is never returned.
* **Downloads are confined.** Every download is saved into the backend's own
  download directory with a sanitised filename and a size cap; a download that
  would exceed the cap is aborted.
* **Dialogs are never auto-accepted.** A ``dialog`` listener records the dialog
  and leaves it open; only an explicit tool call accepts or dismisses it.
* **Honest availability.** If Playwright or a Chromium binary is missing, the
  backend reports itself unavailable with a reason instead of failing on first
  use.

Playwright is an optional dependency: the module imports it lazily so the
platform runs (with an honest null backend) when it is absent.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
import shutil
import time
from pathlib import Path
from typing import Any

from ...core.errors import (
    ExternalServiceError,
    NotFoundError,
    TimeoutError_,
    ValidationError,
)
from ..base import (
    BrowserBackend,
    BrowserPageState,
    DialogInfo,
    DownloadedFile,
    ElementInfo,
    FormFieldInfo,
    FormInfo,
    PageSnapshot,
    Screenshot,
    TabInfo,
)

#: JavaScript that tags interactive elements with a stable ref and returns a
#: bounded, normalised description of the page. Kept as one evaluated function
#: so a single round-trip yields everything an observation needs.
_EXTRACT_JS = r"""
(maxElements) => {
  const maxLinks = 40;
  const refAttr = 'data-aap-ref';
  document.querySelectorAll('[' + refAttr + ']').forEach(e => e.removeAttribute(refAttr));
  let counter = 0;

  const visible = (el) => {
    const r = el.getBoundingClientRect();
    const style = window.getComputedStyle(el);
    return r.width > 0 && r.height > 0 && style.visibility !== 'hidden' &&
           style.display !== 'none' && style.opacity !== '0';
  };
  const accessibleName = (el) => {
    const aria = el.getAttribute('aria-label');
    if (aria) return aria.trim();
    const labelledby = el.getAttribute('aria-labelledby');
    if (labelledby) {
      const target = document.getElementById(labelledby);
      if (target && target.innerText) return target.innerText.trim();
    }
    const tag = el.tagName.toLowerCase();
    if (tag === 'input' || tag === 'select' || tag === 'textarea') {
      if (el.labels && el.labels.length) {
        const text = Array.from(el.labels).map(l => l.innerText).join(' ').trim();
        if (text) return text;
      }
      return (el.getAttribute('placeholder') || el.getAttribute('title') ||
              el.getAttribute('name') || '').trim();
    }
    return (el.innerText || el.textContent || el.getAttribute('title') || '').trim();
  };
  const roleOf = (el) => {
    const explicit = el.getAttribute('role');
    if (explicit) return explicit;
    const tag = el.tagName.toLowerCase();
    const type = (el.getAttribute('type') || '').toLowerCase();
    if (tag === 'a') return 'link';
    if (tag === 'button') return 'button';
    if (tag === 'select') return 'combobox';
    if (tag === 'textarea') return 'textbox';
    if (tag === 'input') {
      if (type === 'submit' || type === 'button' || type === 'reset') return 'button';
      if (type === 'checkbox') return 'checkbox';
      if (type === 'radio') return 'radio';
      if (type === 'file') return 'file';
      return 'textbox';
    }
    if (tag === 'h1' || tag === 'h2' || tag === 'h3' || tag === 'h4' ||
        tag === 'h5' || tag === 'h6') return 'heading';
    return tag;
  };
  const describe = (el) => {
    // Reuse the ref if this element was already tagged, so a field that appears
    // both in the elements list and in a form keeps one stable ref.
    let ref = el.getAttribute(refAttr);
    if (!ref) {
      ref = 'e' + (counter++);
      el.setAttribute(refAttr, ref);
    }
    const r = el.getBoundingClientRect();
    return {
      ref: ref,
      role: roleOf(el),
      name: accessibleName(el).slice(0, 300),
      tag: el.tagName.toLowerCase(),
      text: (el.innerText || el.textContent || '').trim().slice(0, 300),
      value: (el.value !== undefined && el.value !== null) ? String(el.value).slice(0, 300) : '',
      href: el.href ? String(el.href).slice(0, 1000) : '',
      placeholder: (el.getAttribute('placeholder') || '').slice(0, 200),
      input_type: (el.getAttribute('type') || '').toLowerCase(),
      checked: (typeof el.checked === 'boolean') ? el.checked : null,
      disabled: !!el.disabled,
      box: [Math.round(r.x), Math.round(r.y), Math.round(r.width), Math.round(r.height)],
    };
  };

  const selector = 'a, button, input, select, textarea, [role], [contenteditable="true"],' +
                   ' [onclick], summary';
  const candidates = Array.from(document.querySelectorAll(selector)).filter(visible);

  const elements = [];
  const links = [];
  const seenLinks = new Set();
  for (const el of candidates) {
    if (elements.length >= maxElements && links.length >= maxLinks) break;
    const info = describe(el);
    if (info.role === 'link') {
      if (!seenLinks.has(info.href) && links.length < maxLinks) {
        seenLinks.add(info.href);
        links.push(info);
      }
    } else if (elements.length < maxElements) {
      elements.push(info);
    }
  }

  const headings = Array.from(document.querySelectorAll('h1,h2,h3,h4,h5,h6'))
    .map(h => (h.innerText || '').trim()).filter(t => t).slice(0, 20);

  const forms = [];
  for (const form of Array.from(document.querySelectorAll('form')).slice(0, 10)) {
    const fields = [];
    for (const el of Array.from(form.querySelectorAll('input, select, textarea'))) {
      const info = describe(el);
      const options = el.tagName.toLowerCase() === 'select'
        ? Array.from(el.options).map(o => o.value).slice(0, 50) : [];
      fields.push({
        ref: info.ref, name: el.getAttribute('name') || '',
        label: accessibleName(el).slice(0, 200), input_type: info.input_type || 'text',
        value: info.value, placeholder: info.placeholder, required: !!el.required,
        options: options, checked: info.checked,
      });
    }
    forms.push({
      ref: 'form' + forms.length, name: form.getAttribute('name') || '',
      action: form.getAttribute('action') || '',
      method: (form.getAttribute('method') || 'get').toLowerCase(),
      fields: fields,
    });
  }

  const scrollY = window.scrollY || window.pageYOffset || 0;
  return {
    url: location.href, title: document.title,
    text: (document.body ? document.body.innerText : '').slice(0, 200000),
    headings: headings, elements: elements, links: links, forms: forms,
    iframe_count: document.querySelectorAll('iframe').length,
    scroll_y: scrollY,
    scroll_height: (document.documentElement ? document.documentElement.scrollHeight : 0),
    viewport_height: window.innerHeight || 0,
  };
}
"""


def _sanitize_filename(name: str) -> str:
    """Make a download filename safe: no path separators, no traversal."""
    base = Path(name).name  # drops any directory component
    base = re.sub(r"[^\w.\- ]+", "_", base, flags=re.UNICODE).strip(" .")
    if not base:
        base = "download"
    return base[:180]


class PlaywrightBrowserBackend(BrowserBackend):
    """A Chromium backend driven by Playwright."""

    name = "playwright"

    def __init__(
        self,
        *,
        headless: bool = True,
        executable_path: str = "",
        download_dir: str,
        max_download_bytes: int = 64 * 1024 * 1024,
        timeout_ms: int = 15_000,
        user_agent: str = "",
        launch_args: tuple[str, ...] = ("--no-sandbox", "--disable-dev-shm-usage"),
    ) -> None:
        self.headed = not headless
        self._headless = headless
        self._download_dir = Path(download_dir)
        self._max_download_bytes = max_download_bytes
        self._timeout_ms = timeout_ms
        self._launch_args = list(launch_args)
        self._user_agent = user_agent

        self._playwright: Any = None
        self._browser: Any = None
        self._context: Any = None
        self._pages: dict[str, Any] = {}
        self._active_id: str = ""
        self._counter = 0
        self._pending_dialog: Any = None
        self._dialog_info: DialogInfo | None = None

        try:
            import playwright.async_api  # noqa: F401
        except Exception as exc:  # pragma: no cover - environment dependent
            self.available = False
            self.unavailable_reason = f"playwright is not installed: {exc}"
            self._executable = ""
            return

        self._executable = executable_path or self._find_chromium()
        if not self._executable:
            self.available = False
            self.unavailable_reason = (
                "no Chromium binary found (set AAP_BROWSER_EXECUTABLE_PATH or run "
                "`playwright install chromium`)"
            )
            return
        self.available = True
        self.unavailable_reason = ""

    @staticmethod
    def _find_chromium() -> str:
        for candidate in (
            "chromium",
            "chromium-browser",
            "google-chrome",
            "google-chrome-stable",
            "chrome",
            "msedge",
        ):
            found = shutil.which(candidate)
            if found:
                return found
        return ""

    # -- Lifecycle -------------------------------------------------------
    async def start(self) -> None:
        self.require_available()
        if self._browser is not None:
            return
        from playwright.async_api import async_playwright

        self._download_dir.mkdir(parents=True, exist_ok=True)
        try:
            self._playwright = await async_playwright().start()
            self._browser = await self._playwright.chromium.launch(
                headless=self._headless,
                executable_path=self._executable or None,
                args=self._launch_args,
            )
            self._context = await self._browser.new_context(
                accept_downloads=True,
                user_agent=self._user_agent or None,
            )
            self._context.set_default_timeout(self._timeout_ms)
        except Exception as exc:  # pragma: no cover - environment dependent
            await self.aclose()
            raise ExternalServiceError(
                f"browser failed to launch: {exc}", details={"backend": self.name}
            ) from exc
        await self.open_tab()

    async def aclose(self) -> None:
        for page in list(self._pages.values()):
            with contextlib.suppress(Exception):
                await page.close()
        self._pages.clear()
        self._active_id = ""
        if self._context is not None:
            with contextlib.suppress(Exception):
                await self._context.close()
            self._context = None
        if self._browser is not None:
            with contextlib.suppress(Exception):
                await self._browser.close()
            self._browser = None
        if self._playwright is not None:
            with contextlib.suppress(Exception):
                await self._playwright.stop()
            self._playwright = None

    # -- Internal helpers ------------------------------------------------
    def _require_started(self) -> None:
        self.require_available()
        if self._browser is None or self._context is None:
            raise ExternalServiceError(
                "browser has not been started", details={"backend": self.name}
            )

    def _page(self, tab_id: str | None = None) -> Any:
        self._require_started()
        key = tab_id or self._active_id
        page = self._pages.get(key)
        if page is None:
            raise NotFoundError("no such tab", details={"tab_id": key})
        return page

    def _register_page(self, page: Any) -> str:
        self._counter += 1
        tab_id = f"tab{self._counter}"
        self._pages[tab_id] = page
        page.on("dialog", self._on_dialog)
        return tab_id

    def _on_dialog(self, dialog: Any) -> None:
        # Record the dialog and leave it open; only an explicit tool call
        # accepts or dismisses it. Playwright auto-dismisses when no listener is
        # attached, so attaching one is what keeps a consequential confirm dialog
        # from being silently accepted.
        self._pending_dialog = dialog
        try:
            self._dialog_info = DialogInfo(type=dialog.type, message=dialog.message)
        except Exception:  # pragma: no cover - defensive
            self._dialog_info = DialogInfo(type="unknown", message="")

    async def _tab_info(self, tab_id: str) -> TabInfo:
        page = self._pages.get(tab_id)
        if page is None:
            return TabInfo(id=tab_id, state=BrowserPageState.CLOSED)
        try:
            url = page.url
            title = await page.title()
        except Exception:
            return TabInfo(id=tab_id, state=BrowserPageState.CLOSED)
        state = BrowserPageState.READY if url and url != "about:blank" else BrowserPageState.EMPTY
        return TabInfo(
            id=tab_id, url=url, title=title, active=tab_id == self._active_id, state=state
        )

    async def _settle(self, page: Any, *, timeout_ms: int | None = None) -> None:
        """Wait for the page to be usable without an arbitrary long sleep."""
        # A page that never fires domcontentloaded (some SPAs) is still usable;
        # fall through rather than failing the whole navigation.
        with contextlib.suppress(Exception):
            await page.wait_for_load_state(
                "domcontentloaded", timeout=timeout_ms or self._timeout_ms
            )

    # -- Tabs ------------------------------------------------------------
    async def list_tabs(self) -> list[TabInfo]:
        self._require_started()
        return [await self._tab_info(tab_id) for tab_id in self._pages]

    async def open_tab(self, url: str = "") -> TabInfo:
        self._require_started()
        page = await self._context.new_page()
        tab_id = self._register_page(page)
        self._pages[tab_id] = page
        self._active_id = tab_id
        if url:
            await page.goto(url, wait_until="domcontentloaded", timeout=self._timeout_ms)
            await self._settle(page)
        return await self._tab_info(tab_id)

    async def switch_tab(self, tab_id: str) -> TabInfo:
        self._page(tab_id)
        self._active_id = tab_id
        return await self._tab_info(tab_id)

    async def close_tab(self, tab_id: str) -> None:
        page = self._pages.pop(tab_id, None)
        if page is None:
            raise NotFoundError("no such tab", details={"tab_id": tab_id})
        with contextlib.suppress(Exception):
            await page.close()
        if self._active_id == tab_id:
            self._active_id = next(iter(self._pages), "")
            if not self._pages:
                # Never leave the browser with zero tabs; open a fresh one.
                await self.open_tab()

    async def active_tab(self) -> TabInfo:
        self._require_started()
        return await self._tab_info(self._active_id)

    # -- Navigation ------------------------------------------------------
    async def navigate(self, url: str, *, timeout_ms: int | None = None) -> TabInfo:
        page = self._page()
        try:
            await page.goto(
                url, wait_until="domcontentloaded", timeout=timeout_ms or self._timeout_ms
            )
        except Exception as exc:
            if "Timeout" in type(exc).__name__:
                raise TimeoutError_(f"navigation timed out: {url}", details={"url": url}) from exc
            raise ExternalServiceError(f"navigation failed: {exc}", details={"url": url}) from exc
        await self._settle(page, timeout_ms=timeout_ms)
        return await self._tab_info(self._active_id)

    async def go_back(self) -> TabInfo:
        page = self._page()
        try:
            await page.go_back(wait_until="domcontentloaded", timeout=self._timeout_ms)
        except Exception as exc:
            raise ExternalServiceError(f"go_back failed: {exc}") from exc
        return await self._tab_info(self._active_id)

    async def go_forward(self) -> TabInfo:
        page = self._page()
        try:
            await page.go_forward(wait_until="domcontentloaded", timeout=self._timeout_ms)
        except Exception as exc:
            raise ExternalServiceError(f"go_forward failed: {exc}") from exc
        return await self._tab_info(self._active_id)

    async def reload(self) -> TabInfo:
        page = self._page()
        try:
            await page.reload(wait_until="domcontentloaded", timeout=self._timeout_ms)
        except Exception as exc:
            raise ExternalServiceError(f"reload failed: {exc}") from exc
        return await self._tab_info(self._active_id)

    # -- Observation -----------------------------------------------------
    async def snapshot(self, *, max_elements: int = 60) -> PageSnapshot:
        page = self._page()
        try:
            raw = await page.evaluate(_EXTRACT_JS, max_elements)
        except Exception as exc:
            raise ExternalServiceError(f"page observation failed: {exc}") from exc

        elements = tuple(ElementInfo(**self._element_kwargs(e)) for e in raw.get("elements", []))
        links = tuple(ElementInfo(**self._element_kwargs(e)) for e in raw.get("links", []))
        forms = tuple(
            FormInfo(
                ref=f["ref"],
                name=f.get("name", ""),
                action=f.get("action", ""),
                method=f.get("method", "get"),
                fields=tuple(
                    FormFieldInfo(
                        ref=field["ref"],
                        name=field.get("name", ""),
                        label=field.get("label", ""),
                        input_type=field.get("input_type", "text"),
                        value=field.get("value", ""),
                        placeholder=field.get("placeholder", ""),
                        required=bool(field.get("required", False)),
                        options=tuple(field.get("options", ())),
                        checked=field.get("checked"),
                    )
                    for field in f.get("fields", [])
                ),
            )
            for f in raw.get("forms", [])
        )
        text = raw.get("text", "") or ""
        return PageSnapshot(
            url=raw.get("url", ""),
            title=raw.get("title", ""),
            state=BrowserPageState.READY,
            text=text,
            text_truncated=len(text) > 200_000,
            headings=tuple(raw.get("headings", ())),
            elements=elements,
            forms=forms,
            links=links,
            dialog=self._dialog_info,
            iframe_count=int(raw.get("iframe_count", 0)),
            scroll_y=float(raw.get("scroll_y", 0.0)),
            scroll_height=float(raw.get("scroll_height", 0.0)),
            viewport_height=float(raw.get("viewport_height", 0.0)),
        )

    @staticmethod
    def _element_kwargs(e: dict[str, Any]) -> dict[str, Any]:
        box = e.get("box")
        return {
            "ref": e["ref"],
            "role": e.get("role", ""),
            "name": e.get("name", ""),
            "tag": e.get("tag", ""),
            "text": e.get("text", ""),
            "value": e.get("value", ""),
            "href": e.get("href", ""),
            "placeholder": e.get("placeholder", ""),
            "input_type": e.get("input_type", ""),
            "checked": e.get("checked"),
            "disabled": bool(e.get("disabled", False)),
            "box": tuple(box) if box else None,
        }

    async def screenshot(self, *, full_page: bool = False) -> Screenshot:
        page = self._page()
        try:
            data = await page.screenshot(full_page=full_page, type="png")
            size = page.viewport_size or {}
        except Exception as exc:
            raise ExternalServiceError(f"screenshot failed: {exc}") from exc
        return Screenshot(
            data=bytes(data),
            mime_type="image/png",
            width=int(size.get("width", 0) or 0),
            height=int(size.get("height", 0) or 0),
        )

    # -- Interaction -----------------------------------------------------
    def _locator(self, ref: str) -> Any:
        page = self._page()
        return page.locator(f'[data-aap-ref="{ref}"]')

    async def click(self, ref: str, *, timeout_ms: int | None = None) -> None:
        timeout = timeout_ms or self._timeout_ms
        locator = self._locator(ref)
        # A click that opens a JavaScript dialog never resolves in Playwright
        # until the dialog is handled, which would wedge the run. Race the click
        # against dialog detection: if a dialog appears, return immediately and
        # leave the dialog open for the agent to resolve explicitly (so a
        # consequential confirm is never silently accepted).
        click_task = asyncio.ensure_future(locator.click(timeout=timeout))
        dialog_task = asyncio.ensure_future(self._wait_for_dialog(timeout))
        try:
            await asyncio.wait({click_task, dialog_task}, return_when=asyncio.FIRST_COMPLETED)
            if self._pending_dialog is not None:
                # A dialog opened: return promptly and leave it open for the
                # agent to resolve explicitly. Playwright's click stays blocked
                # until then, so it is cancelled below.
                return
            # No dialog: the click either completed or timed out; surface it.
            await click_task
        finally:
            for task in (click_task, dialog_task):
                if not task.done():
                    task.cancel()
            await asyncio.gather(click_task, dialog_task, return_exceptions=True)

    async def _wait_for_dialog(self, timeout_ms: int) -> None:
        """Return as soon as a dialog is pending, or when ``timeout_ms`` elapses."""
        deadline = time.monotonic() + timeout_ms / 1000
        while self._pending_dialog is None:
            if time.monotonic() >= deadline:
                return
            await asyncio.sleep(0.05)

    async def type_text(self, ref: str, text: str, *, clear: bool = True) -> None:
        locator = self._locator(ref)
        try:
            if clear:
                await locator.fill(text, timeout=self._timeout_ms)
            else:
                await locator.press_sequentially(text, timeout=self._timeout_ms)
        except Exception as exc:
            raise self._interaction_error("type", ref, exc) from exc

    async def press_key(self, key: str, *, ref: str | None = None) -> None:
        try:
            if ref:
                await self._locator(ref).press(key, timeout=self._timeout_ms)
            else:
                await self._page().keyboard.press(key)
        except Exception as exc:
            raise self._interaction_error("press", ref or "", exc) from exc

    async def select_option(self, ref: str, value: str) -> None:
        locator = self._locator(ref)
        try:
            try:
                await locator.select_option(value=value, timeout=self._timeout_ms)
            except Exception:
                await locator.select_option(label=value, timeout=self._timeout_ms)
        except Exception as exc:
            raise self._interaction_error("select", ref, exc) from exc

    async def set_checked(self, ref: str, *, checked: bool) -> None:
        locator = self._locator(ref)
        try:
            if checked:
                await locator.check(timeout=self._timeout_ms)
            else:
                await locator.uncheck(timeout=self._timeout_ms)
        except Exception as exc:
            raise self._interaction_error("check", ref, exc) from exc

    async def scroll(self, *, amount: int, ref: str | None = None) -> None:
        try:
            if ref:
                await self._locator(ref).scroll_into_view_if_needed(timeout=self._timeout_ms)
            else:
                await self._page().mouse.wheel(0, amount)
        except Exception as exc:
            raise self._interaction_error("scroll", ref or "", exc) from exc

    async def wait_for(self, *, kind: str, value: str = "", timeout_ms: int | None = None) -> None:
        page = self._page()
        timeout = timeout_ms or self._timeout_ms
        try:
            if kind == "load":
                await page.wait_for_load_state("load", timeout=timeout)
            elif kind == "text":
                await page.get_by_text(value, exact=False).first.wait_for(timeout=timeout)
            elif kind == "url":
                await page.wait_for_url(f"**{value}**", timeout=timeout)
            elif kind == "element":
                await page.locator(value).first.wait_for(timeout=timeout)
            else:
                raise ValidationError(f"unknown wait kind: {kind}")
        except ValidationError:
            raise
        except Exception as exc:
            raise TimeoutError_(
                f"wait for {kind} timed out", details={"kind": kind, "value": value}
            ) from exc

    # -- Files -----------------------------------------------------------
    async def download(self, ref: str, *, timeout_ms: int | None = None) -> DownloadedFile:
        page = self._page()
        locator = self._locator(ref)
        try:
            async with page.expect_download(timeout=timeout_ms or self._timeout_ms) as info:
                await locator.click(timeout=timeout_ms or self._timeout_ms)
            download = await info.value
        except Exception as exc:
            raise self._interaction_error("download", ref, exc) from exc

        suggested = _sanitize_filename(download.suggested_filename or "download")
        target = self._download_dir / suggested
        # Ensure uniqueness without allowing traversal.
        stem, suffix = target.stem, target.suffix
        index = 1
        while target.exists():
            target = self._download_dir / f"{stem}_{index}{suffix}"
            index += 1
        try:
            await download.save_as(str(target))
        except Exception as exc:
            raise ExternalServiceError(f"download failed to save: {exc}") from exc
        size = target.stat().st_size if target.exists() else 0
        if size > self._max_download_bytes:
            target.unlink(missing_ok=True)
            raise ValidationError(
                "download exceeds the permitted size",
                details={"size": size, "max": self._max_download_bytes},
            )
        return DownloadedFile(
            suggested_filename=suggested,
            path=str(target),
            size_bytes=size,
            url=download.url or "",
        )

    async def upload(self, ref: str, path: str) -> None:
        locator = self._locator(ref)
        try:
            await locator.set_input_files(path, timeout=self._timeout_ms)
        except Exception as exc:
            raise self._interaction_error("upload", ref, exc) from exc

    # -- Dialogs ---------------------------------------------------------
    async def dialog(self) -> DialogInfo | None:
        return self._dialog_info

    async def accept_dialog(self, *, prompt_text: str | None = None) -> None:
        dialog = self._pending_dialog
        if dialog is None:
            raise ValidationError("no dialog is open")
        try:
            if prompt_text is not None:
                await dialog.accept(prompt_text)
            else:
                await dialog.accept()
        except Exception as exc:
            raise ExternalServiceError(f"could not accept dialog: {exc}") from exc
        finally:
            self._pending_dialog = None
            self._dialog_info = None

    async def dismiss_dialog(self) -> None:
        dialog = self._pending_dialog
        if dialog is None:
            raise ValidationError("no dialog is open")
        try:
            await dialog.dismiss()
        except Exception as exc:
            raise ExternalServiceError(f"could not dismiss dialog: {exc}") from exc
        finally:
            self._pending_dialog = None
            self._dialog_info = None

    # -- Error mapping ---------------------------------------------------
    @staticmethod
    def _interaction_error(action: str, ref: str, exc: Exception) -> Exception:
        """Map a Playwright failure onto a classified, recoverable error."""
        name = type(exc).__name__
        message = str(exc)
        if "Timeout" in name or "timeout" in message.lower():
            return TimeoutError_(
                f"{action} timed out for {ref or 'element'}",
                details={"action": action, "ref": ref},
            )
        # A detached/not-found element is the classic "page changed" case: the
        # ref is stale, so the agent must re-observe rather than retry blindly.
        return ExternalServiceError(
            f"{action} failed for {ref or 'element'}: {message[:200]}",
            details={"action": action, "ref": ref},
        )


__all__ = ["PlaywrightBrowserBackend"]
