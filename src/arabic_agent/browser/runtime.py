"""The browser runtime: the single object the browser tools call.

It bundles a :class:`BrowserBackend`, the :class:`BrowserPolicy`, a per-run
:class:`ActionBudget`, the :class:`ObservationStore`, the
:class:`WebDataBoundary`, the :class:`SourceLedger` and the audit log, and
enforces the observe -> act -> observe discipline:

* an interaction must reference a *fresh* observation (or the latest one); acting
  with no observation, or on a stale one, is refused, so the agent cannot click a
  ref from before a navigation;
* every action consumes a per-run budget and is bounded by policy (tabs,
  downloads, uploads);
* navigation is validated by the URL/domain policy *before* the browser is
  touched -- an unsafe scheme, a blocked domain or a private address never
  reaches the engine;
* a submitted form is treated as consequential: the runtime refuses a
  ``submit`` unless the tool layer has cleared it, and records the decision;
* a requested stop (cancellation) halts actions and releases resources;
* every action emits an audit event carrying *metadata only* -- never the raw
  page content, never a typed secret, never a screenshot.

It is created once and shared; per-run state is keyed by ``run_id``. Tools reach
it through ``context.metadata["browser_runtime"]``, exactly as the computer tools
reach theirs, so the executor and agent runner stay unaware of it.
"""

from __future__ import annotations

import time

from ..audit.base import AuditEventType, AuditLog
from ..core.errors import PermissionDeniedError, ValidationError
from .base import BrowserBackend, DialogInfo, DownloadedFile, TabInfo
from .boundary import WebDataBoundary
from .observations import BrowserObservation, ObservationStore, build_observation
from .policy import ActionBudget, BrowserPolicy
from .sources import SourceLedger, make_source

#: Interaction kinds that are treated as consequential form submission.
_SUBMIT_KINDS = frozenset({"submit", "accept_dialog"})


class BrowserRuntime:
    """Executes browser actions under policy, budget and audit."""

    def __init__(
        self,
        *,
        backend: BrowserBackend,
        policy: BrowserPolicy,
        audit: AuditLog,
        boundary: WebDataBoundary,
        observations: ObservationStore | None = None,
        sources: SourceLedger | None = None,
    ) -> None:
        self._backend = backend
        self._policy = policy
        self._audit = audit
        self._boundary = boundary
        self._observations = observations or ObservationStore()
        self._sources = sources or SourceLedger()
        self._budgets: dict[str, ActionBudget] = {}
        self._stopped: set[str] = set()
        self._started = False

    # -- Introspection ---------------------------------------------------
    @property
    def backend(self) -> BrowserBackend:
        return self._backend

    @property
    def policy(self) -> BrowserPolicy:
        return self._policy

    @property
    def boundary(self) -> WebDataBoundary:
        return self._boundary

    @property
    def sources(self) -> SourceLedger:
        return self._sources

    @property
    def available(self) -> bool:
        return self._backend.available

    # -- Lifecycle -------------------------------------------------------
    async def ensure_started(self) -> None:
        if not self._started:
            await self._backend.start()
            self._started = True

    async def aclose(self) -> None:
        if self._started:
            await self._backend.aclose()
            self._started = False

    # -- Stop / cancellation --------------------------------------------
    def request_stop(self, run_id: str) -> None:
        self._stopped.add(run_id)

    def is_stopped(self, run_id: str) -> bool:
        return run_id in self._stopped

    def clear_stop(self, run_id: str) -> None:
        self._stopped.discard(run_id)

    def _check_running(self, run_id: str) -> None:
        if self.is_stopped(run_id):
            raise PermissionDeniedError(
                "browser control was stopped for this run", details={"run_id": run_id}
            )

    def _budget(self, run_id: str) -> ActionBudget:
        budget = self._budgets.get(run_id)
        if budget is None:
            budget = ActionBudget(self._policy)
            self._budgets[run_id] = budget
        return budget

    # -- Observation -----------------------------------------------------
    async def observe(
        self,
        *,
        tenant_id: str,
        user_id: str,
        run_id: str,
        observation_id: str,
        include_screenshot: bool = False,
        record_source: bool = True,
    ) -> BrowserObservation:
        """Observe the active page and store a bounded, freshness-tracked view."""
        self._check_running(run_id)
        await self.ensure_started()
        self._budget(run_id).consume("observe", now=time.monotonic())
        snapshot = await self._backend.snapshot(max_elements=self._policy.max_elements)
        # A local capture is always permitted by the boundary; the *routing* of
        # the resulting image to a model is what the boundary constrains (see
        # BrowserAttachmentProvider).
        screenshot = await self._backend.screenshot() if include_screenshot else None
        observation = build_observation(
            observation_id=observation_id,
            run_id=run_id,
            snapshot=snapshot,
            max_text_chars=self._policy.max_text_chars,
            screenshot=screenshot,
        )
        # The tab id is part of the observation identity.
        tab = await self._backend.active_tab()
        observation = observation.model_copy(update={"tab_id": tab.id})
        self._observations.put(observation)

        if record_source and observation.url:
            self._sources.record(
                run_id,
                make_source(
                    url=observation.url,
                    title=observation.title,
                    locator=observation.headings[0] if observation.headings else "",
                    excerpt=observation.text[:300],
                    observation_id=observation.id,
                ),
            )

        await self._audit.record(
            AuditEventType.BROWSER_OBSERVED,
            tenant_id=tenant_id,
            user_id=user_id,
            run_id=run_id,
            message="page observed",
            data={
                "observation_id": observation.id,
                "url": observation.url,
                "title": observation.title,
                "tab_id": observation.tab_id,
                "elements": len(observation.elements),
                "forms": len(observation.forms),
                "injection_indicators": [i.kind.value for i in observation.injection],
                "web_policy": self._boundary.policy.value,
                "screenshot": screenshot is not None,
            },
        )
        return observation

    def get_observation(self, observation_id: str, *, run_id: str) -> BrowserObservation:
        return self._observations.get(observation_id, run_id=run_id)

    def latest_observation(self, *, run_id: str) -> BrowserObservation | None:
        return self._observations.latest(run_id=run_id)

    def _require_observation(self, run_id: str, observation_id: str | None) -> BrowserObservation:
        observation: BrowserObservation | None
        if observation_id is not None:
            observation = self._observations.get(observation_id, run_id=run_id)
        else:
            observation = self._observations.latest(run_id=run_id)
        if observation is None:
            raise ValidationError("no page observation is available; observe the page first")
        if self._policy.require_fresh_observation and observation.is_stale(
            max_age=self._policy.max_observation_age_seconds
        ):
            raise ValidationError(
                "the referenced observation is stale; re-observe before acting",
                details={
                    "observation_id": observation.id,
                    "age": round(observation.age_seconds, 1),
                },
            )
        return observation

    # -- Navigation ------------------------------------------------------
    async def navigate(
        self,
        *,
        tenant_id: str,
        user_id: str,
        run_id: str,
        url: str,
        observation_id: str,
        record_source: bool = True,
    ) -> BrowserObservation:
        """Validate ``url`` against policy, navigate, and return a fresh view."""
        self._check_running(run_id)
        risk = self._policy.check_navigation(url)
        await self.ensure_started()
        self._budget(run_id).consume("navigate", now=time.monotonic())
        tab = await self._backend.navigate(url, timeout_ms=self._policy.default_timeout_ms)
        await self._audit.record(
            AuditEventType.BROWSER_NAVIGATED,
            tenant_id=tenant_id,
            user_id=user_id,
            run_id=run_id,
            message="navigated",
            data={"url": tab.url, "risk": risk.value, "tab_id": tab.id},
        )
        return await self.observe(
            tenant_id=tenant_id,
            user_id=user_id,
            run_id=run_id,
            observation_id=observation_id,
            record_source=record_source,
        )

    async def open_tab(
        self, *, tenant_id: str, user_id: str, run_id: str, url: str, observation_id: str
    ) -> BrowserObservation:
        self._check_running(run_id)
        if url:
            self._policy.check_navigation(url)
        await self.ensure_started()
        tabs = await self._backend.list_tabs()
        if len(tabs) >= self._policy.max_tabs:
            raise ValidationError(
                "tab limit reached; close a tab before opening another",
                details={"max_tabs": self._policy.max_tabs},
            )
        self._budget(run_id).consume("navigate", now=time.monotonic())
        await self._backend.open_tab(url)
        await self._audit.record(
            AuditEventType.BROWSER_TAB_OPENED,
            tenant_id=tenant_id,
            user_id=user_id,
            run_id=run_id,
            message="tab opened",
            data={"url": url},
        )
        return await self.observe(
            tenant_id=tenant_id, user_id=user_id, run_id=run_id, observation_id=observation_id
        )

    async def switch_tab(
        self, *, tenant_id: str, user_id: str, run_id: str, tab_id: str, observation_id: str
    ) -> BrowserObservation:
        self._check_running(run_id)
        await self.ensure_started()
        await self._backend.switch_tab(tab_id)
        await self._audit.record(
            AuditEventType.BROWSER_TAB_SWITCHED,
            tenant_id=tenant_id,
            user_id=user_id,
            run_id=run_id,
            message="tab switched",
            data={"tab_id": tab_id},
        )
        return await self.observe(
            tenant_id=tenant_id, user_id=user_id, run_id=run_id, observation_id=observation_id
        )

    async def close_tab(self, *, tenant_id: str, user_id: str, run_id: str, tab_id: str) -> None:
        self._check_running(run_id)
        await self.ensure_started()
        await self._backend.close_tab(tab_id)
        await self._audit.record(
            AuditEventType.BROWSER_TAB_CLOSED,
            tenant_id=tenant_id,
            user_id=user_id,
            run_id=run_id,
            message="tab closed",
            data={"tab_id": tab_id},
        )

    async def list_tabs(self, *, run_id: str) -> list[TabInfo]:
        self._check_running(run_id)
        await self.ensure_started()
        return await self._backend.list_tabs()

    async def go_back(
        self, *, tenant_id: str, user_id: str, run_id: str, observation_id: str
    ) -> BrowserObservation:
        self._check_running(run_id)
        await self.ensure_started()
        self._budget(run_id).consume("navigate", now=time.monotonic())
        await self._backend.go_back()
        return await self.observe(
            tenant_id=tenant_id, user_id=user_id, run_id=run_id, observation_id=observation_id
        )

    async def go_forward(
        self, *, tenant_id: str, user_id: str, run_id: str, observation_id: str
    ) -> BrowserObservation:
        self._check_running(run_id)
        await self.ensure_started()
        self._budget(run_id).consume("navigate", now=time.monotonic())
        await self._backend.go_forward()
        return await self.observe(
            tenant_id=tenant_id, user_id=user_id, run_id=run_id, observation_id=observation_id
        )

    async def reload(
        self, *, tenant_id: str, user_id: str, run_id: str, observation_id: str
    ) -> BrowserObservation:
        self._check_running(run_id)
        await self.ensure_started()
        self._budget(run_id).consume("navigate", now=time.monotonic())
        await self._backend.reload()
        return await self.observe(
            tenant_id=tenant_id, user_id=user_id, run_id=run_id, observation_id=observation_id
        )

    # -- Interaction -----------------------------------------------------
    async def act(
        self,
        *,
        tenant_id: str,
        user_id: str,
        run_id: str,
        observation_id: str,
        action: str,
        ref: str = "",
        text: str = "",
        value: str = "",
        key: str = "",
        checked: bool | None = None,
        amount: int = 0,
        approved_submit: bool = False,
        new_observation_id: str = "",
    ) -> BrowserObservation:
        """Perform one interaction against a fresh observation, then re-observe.

        ``action`` is one of ``click``, ``type``, ``press``, ``select``,
        ``check``, ``scroll``, ``submit``. The observation is re-taken afterwards
        so the agent always reasons about the page's *new* state.
        """
        self._check_running(run_id)
        observation = self._require_observation(run_id, observation_id or None)

        if (
            action in _SUBMIT_KINDS
            and self._policy.require_approval_for_submit
            and not approved_submit
        ):
            raise PermissionDeniedError(
                "submitting a form requires approval before the final action",
                details={"action": action},
            )
        if ref and observation.element(ref) is None:
            raise ValidationError(
                "the referenced element is not in the current observation; re-observe",
                details={"ref": ref, "observation_id": observation.id},
            )

        await self.ensure_started()
        self._budget(run_id).consume("interact", now=time.monotonic())

        if action == "click":
            await self._backend.click(ref, timeout_ms=self._policy.default_timeout_ms)
        elif action == "type":
            if not text:
                raise ValidationError("type requires text")
            await self._backend.type_text(ref, text)
        elif action == "press":
            await self._backend.press_key(key, ref=ref or None)
        elif action == "select":
            if not value:
                raise ValidationError("select requires a value")
            await self._backend.select_option(ref, value)
        elif action == "check":
            await self._backend.set_checked(ref, checked=bool(checked))
        elif action == "scroll":
            await self._backend.scroll(amount=amount, ref=ref or None)
        elif action == "submit":
            # Submitting is the same click, but it has already cleared the
            # approval backstop above and is audited distinctly.
            await self._backend.click(ref, timeout_ms=self._policy.default_timeout_ms)
        else:
            raise ValidationError(f"unknown browser action: {action}")

        await self._audit.record(
            AuditEventType.BROWSER_INTERACTED,
            tenant_id=tenant_id,
            user_id=user_id,
            run_id=run_id,
            message=f"browser action: {action}",
            data={
                "action": action,
                "ref": ref,
                "observation_id": observation.id,
                # Length only for typed text: it may be a secret.
                "text_length": len(text),
                "value": value,
                "key": key,
            },
        )
        if action == "submit":
            await self._audit.record(
                AuditEventType.BROWSER_FORM_SUBMITTED,
                tenant_id=tenant_id,
                user_id=user_id,
                run_id=run_id,
                message="form submitted",
                data={"ref": ref, "observation_id": observation.id},
            )
        # A click/press can open a JavaScript dialog that freezes page script; the
        # backend returns promptly in that case and we must not attempt a DOM
        # snapshot until the dialog is resolved.
        dialog = await self._backend.dialog()
        if dialog is not None:
            return observation.model_copy(update={"dialog": dialog})
        return await self.observe(
            tenant_id=tenant_id,
            user_id=user_id,
            run_id=run_id,
            observation_id=new_observation_id or observation_id,
        )

    async def wait(
        self,
        *,
        tenant_id: str,
        user_id: str,
        run_id: str,
        observation_id: str,
        kind: str,
        value: str = "",
        timeout_ms: int | None = None,
    ) -> BrowserObservation:
        self._check_running(run_id)
        await self.ensure_started()
        await self._backend.wait_for(
            kind=kind, value=value, timeout_ms=timeout_ms or self._policy.default_timeout_ms
        )
        await self._audit.record(
            AuditEventType.BROWSER_WAITED,
            tenant_id=tenant_id,
            user_id=user_id,
            run_id=run_id,
            message=f"waited for {kind}",
            data={"kind": kind, "value": value},
        )
        return await self.observe(
            tenant_id=tenant_id, user_id=user_id, run_id=run_id, observation_id=observation_id
        )

    # -- Files -----------------------------------------------------------
    async def download(
        self,
        *,
        tenant_id: str,
        user_id: str,
        run_id: str,
        observation_id: str,
        ref: str,
    ) -> tuple[DownloadedFile, BrowserObservation]:
        self._check_running(run_id)
        observation = self._require_observation(run_id, observation_id or None)
        if observation.element(ref) is None:
            raise ValidationError("the referenced download element is not in the observation")
        await self.ensure_started()
        self._budget(run_id).consume("download", now=time.monotonic())
        file = await self._backend.download(ref, timeout_ms=self._policy.default_timeout_ms)
        await self._audit.record(
            AuditEventType.BROWSER_DOWNLOADED,
            tenant_id=tenant_id,
            user_id=user_id,
            run_id=run_id,
            message="file downloaded",
            data={
                "filename": file.suggested_filename,
                "size_bytes": file.size_bytes,
                "ref": ref,
            },
        )
        fresh = await self.observe(
            tenant_id=tenant_id, user_id=user_id, run_id=run_id, observation_id=observation_id
        )
        return file, fresh

    async def upload(
        self,
        *,
        tenant_id: str,
        user_id: str,
        run_id: str,
        observation_id: str,
        ref: str,
        path: str,
    ) -> BrowserObservation:
        self._check_running(run_id)
        observation = self._require_observation(run_id, observation_id or None)
        if observation.element(ref) is None:
            raise ValidationError("the referenced upload element is not in the observation")
        await self.ensure_started()
        self._budget(run_id).consume("upload", now=time.monotonic())
        await self._backend.upload(ref, path)
        await self._audit.record(
            AuditEventType.BROWSER_UPLOADED,
            tenant_id=tenant_id,
            user_id=user_id,
            run_id=run_id,
            message="file uploaded",
            # Path basename only; the full local path is not needed to audit.
            data={"ref": ref, "filename": path.rsplit("/", 1)[-1]},
        )
        return await self.observe(
            tenant_id=tenant_id, user_id=user_id, run_id=run_id, observation_id=observation_id
        )

    # -- Dialogs ---------------------------------------------------------
    async def dialog(self, *, tenant_id: str, user_id: str, run_id: str) -> DialogInfo | None:
        self._check_running(run_id)
        await self.ensure_started()
        return await self._backend.dialog()

    async def resolve_dialog(
        self,
        *,
        tenant_id: str,
        user_id: str,
        run_id: str,
        observation_id: str,
        accept: bool,
        prompt_text: str | None = None,
        approved: bool = False,
    ) -> BrowserObservation:
        self._check_running(run_id)
        await self.ensure_started()
        if accept and self._policy.require_approval_for_submit and not approved:
            # Accepting a confirm/prompt can be consequential; require the same
            # approval backstop as a form submit.
            raise PermissionDeniedError(
                "accepting a dialog requires approval", details={"action": "accept_dialog"}
            )
        if accept:
            await self._backend.accept_dialog(prompt_text=prompt_text)
        else:
            await self._backend.dismiss_dialog()
        await self._audit.record(
            AuditEventType.BROWSER_DIALOG_RESOLVED,
            tenant_id=tenant_id,
            user_id=user_id,
            run_id=run_id,
            message="dialog accepted" if accept else "dialog dismissed",
            data={"accept": accept},
        )
        return await self.observe(
            tenant_id=tenant_id, user_id=user_id, run_id=run_id, observation_id=observation_id
        )

    # -- Research --------------------------------------------------------
    async def note_source(
        self,
        *,
        run_id: str,
        url: str,
        title: str = "",
        locator: str = "",
        excerpt: str = "",
        observation_id: str = "",
    ) -> None:
        """Record a source the agent read, for citation in the final answer."""
        self._sources.record(
            run_id,
            make_source(
                url=url,
                title=title,
                locator=locator,
                excerpt=excerpt,
                observation_id=observation_id,
            ),
        )

    def source_list(self, *, run_id: str) -> list[dict[str, object]]:
        return self._sources.render(run_id)

    async def changed_since(
        self,
        *,
        tenant_id: str,
        user_id: str,
        run_id: str,
        observation_id: str,
        new_observation_id: str,
    ) -> tuple[BrowserObservation, bool]:
        """Re-observe and report whether the page changed materially."""
        previous = self._observations.get(observation_id, run_id=run_id)
        fresh = await self.observe(
            tenant_id=tenant_id,
            user_id=user_id,
            run_id=run_id,
            observation_id=new_observation_id,
            record_source=False,
        )
        changed = previous.change_hash != fresh.change_hash
        await self._audit.record(
            AuditEventType.BROWSER_ACTION_VERIFIED,
            tenant_id=tenant_id,
            user_id=user_id,
            run_id=run_id,
            message="page change verified" if changed else "no material page change",
            data={
                "previous_observation_id": previous.id,
                "observation_id": fresh.id,
                "changed": changed,
            },
        )
        return fresh, changed


__all__ = ["BrowserRuntime"]
