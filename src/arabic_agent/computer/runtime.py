"""The computer runtime: the single object the computer tools call.

It bundles a :class:`ComputerBackend`, a :class:`PerceptionLayer`, the
:class:`ComputerPolicy`, a per-run :class:`ActionBudget`, the
:class:`ObservationStore`, the screen-data boundary and the audit log, and
enforces the observe → act → observe discipline:

* an action must reference a *fresh* observation (or the latest one); acting
  with no observation, or on a stale one, is refused;
* every action consumes a per-run budget and is bounded by policy;
* an out-of-bounds point is clamped or refused, never sent blindly;
* a requested stop (cancellation or the emergency stop) halts input before the
  next action and releases nothing held;
* every action emits an audit event carrying *metadata only* — never the raw
  screenshot and never a typed secret.

It is created once and shared; per-run state is keyed by ``run_id``. Tools reach
it through ``context.metadata["computer_runtime"]``, exactly as local filesystem
tools reach their sandbox, so the executor and agent runner stay unaware of it.
"""

from __future__ import annotations

import time

from ..audit.base import AuditEventType, AuditLog
from ..core.errors import PermissionDeniedError, ValidationError
from .base import ComputerBackend, PointerButton, WindowInfo
from .grounding import GroundingTarget, ResolvedTarget, resolve_target
from .observations import ObservationStore
from .perception import ChangeDetector, Observation, PerceptionLayer
from .policy import ActionBudget, ComputerPolicy
from .screen_boundary import ScreenDataBoundary


class ComputerRuntime:
    """Executes computer actions under policy, budget and audit."""

    def __init__(
        self,
        *,
        backend: ComputerBackend,
        perception: PerceptionLayer,
        policy: ComputerPolicy,
        audit: AuditLog,
        boundary: ScreenDataBoundary,
        observations: ObservationStore | None = None,
    ) -> None:
        self._backend = backend
        self._perception = perception
        self._policy = policy
        self._audit = audit
        self._boundary = boundary
        self._observations = observations or ObservationStore()
        self._budgets: dict[str, ActionBudget] = {}
        self._stopped: set[str] = set()
        self._change = ChangeDetector()

    @property
    def backend(self) -> ComputerBackend:
        return self._backend

    @property
    def policy(self) -> ComputerPolicy:
        return self._policy

    @property
    def boundary(self) -> ScreenDataBoundary:
        return self._boundary

    @property
    def supports_windows(self) -> bool:
        return self._backend.supports_windows

    @property
    def ocr_available(self) -> bool:
        return self._perception.ocr_available

    @property
    def accessibility_available(self) -> bool:
        return self._perception.accessibility_available

    # -- Stop / cancellation --------------------------------------------
    def request_stop(self, run_id: str) -> None:
        """Ask the runtime to stop issuing input for ``run_id`` immediately."""
        self._stopped.add(run_id)

    def is_stopped(self, run_id: str) -> bool:
        return run_id in self._stopped

    def clear_stop(self, run_id: str) -> None:
        self._stopped.discard(run_id)

    def _check_running(self, run_id: str) -> None:
        if self.is_stopped(run_id):
            raise PermissionDeniedError(
                "computer control was stopped for this run", details={"run_id": run_id}
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
        monitor_id: int | None = None,
        include_ocr: bool = True,
        include_windows: bool = True,
    ) -> Observation:
        """Capture and structure the screen, enforcing the capture budget."""
        self._check_running(run_id)
        self._budget(run_id).consume("capture", now=time.monotonic())
        observation = await self._perception.observe(
            run_id=run_id,
            monitor_id=monitor_id,
            include_ocr=include_ocr,
            include_windows=include_windows,
        )
        self._observations.put(observation)
        await self._audit.record(
            AuditEventType.COMPUTER_SCREEN_CAPTURED,
            tenant_id=tenant_id,
            user_id=user_id,
            run_id=run_id,
            message="screen captured",
            data={
                "observation_id": observation.id,
                "width": observation.image_width,
                "height": observation.image_height,
                "monitor": None if observation.monitor is None else observation.monitor.id,
                "sources": list(observation.sources),
                "screen_policy": self._boundary.policy.value,
                # Metadata only: never the image, never recognised secrets.
                "active_window": None
                if observation.active_window is None
                else observation.active_window.title,
            },
        )
        return observation

    def get_observation(self, observation_id: str, *, run_id: str) -> Observation:
        return self._observations.get(observation_id, run_id=run_id)

    def latest_observation(self, *, run_id: str) -> Observation | None:
        return self._observations.latest(run_id=run_id)

    def _require_observation(self, run_id: str, observation_id: str | None) -> Observation:
        observation: Observation | None
        if observation_id is not None:
            observation = self._observations.get(observation_id, run_id=run_id)
        else:
            observation = self._observations.latest(run_id=run_id)
        if observation is None:
            raise ValidationError("no screen observation is available; capture the screen first")
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

    def resolve(self, target: GroundingTarget, observation: Observation) -> ResolvedTarget:
        resolved = resolve_target(target, observation)
        monitors = list(observation.metadata.monitors)
        point = self._policy.normalize_point(monitors, resolved.point)
        if point is not resolved.point:
            resolved = resolved.model_copy(update={"point": point})
        return resolved

    async def changed_since(
        self, previous_observation_id: str, *, tenant_id: str, user_id: str, run_id: str
    ) -> tuple[Observation, bool]:
        """Re-observe and report whether the screen changed materially.

        This is the post-action verification primitive: after an action the agent
        calls this, and only treats the action as effective when the screen
        actually changed (or deliberately checks for an expected absence). The
        comparison is an explicit perceptual-hash distance against the previous
        observation, so it does not depend on any hidden detector state.
        """
        from .capture import hamming_distance

        previous = self._observations.get(previous_observation_id, run_id=run_id)
        fresh = await self.observe(tenant_id=tenant_id, user_id=user_id, run_id=run_id)
        changed = hamming_distance(previous.change_hash, fresh.change_hash) > 3
        await self._audit.record(
            AuditEventType.COMPUTER_ACTION_VERIFIED,
            tenant_id=tenant_id,
            user_id=user_id,
            run_id=run_id,
            message="screen change verified" if changed else "no material screen change",
            data={
                "previous_observation_id": previous.id,
                "observation_id": fresh.id,
                "changed": changed,
            },
        )
        return fresh, changed

    # -- Pointer ---------------------------------------------------------
    async def pointer_move(
        self,
        *,
        tenant_id: str,
        user_id: str,
        run_id: str,
        target: GroundingTarget,
        observation_id: str | None = None,
    ) -> ResolvedTarget:
        self._check_running(run_id)
        observation = self._require_observation(run_id, observation_id)
        resolved = self.resolve(target, observation)
        self._budget(run_id).consume("pointer", now=time.monotonic())
        await self._backend.pointer_move(resolved.point)
        await self._audit.record(
            AuditEventType.COMPUTER_POINTER_MOVED,
            tenant_id=tenant_id,
            user_id=user_id,
            run_id=run_id,
            message="pointer moved",
            data={
                "x": resolved.point.x,
                "y": resolved.point.y,
                "matched": resolved.matched,
                "observation_id": observation.id,
            },
        )
        return resolved

    async def pointer_click(
        self,
        *,
        tenant_id: str,
        user_id: str,
        run_id: str,
        target: GroundingTarget,
        button: PointerButton = PointerButton.LEFT,
        double: bool = False,
        observation_id: str | None = None,
    ) -> ResolvedTarget:
        self._check_running(run_id)
        self._policy.check_button(button)
        observation = self._require_observation(run_id, observation_id)
        resolved = self.resolve(target, observation)
        self._budget(run_id).consume("pointer", now=time.monotonic())
        await self._backend.pointer_click(resolved.point, button=button, double=double)
        await self._audit.record(
            AuditEventType.COMPUTER_CLICKED,
            tenant_id=tenant_id,
            user_id=user_id,
            run_id=run_id,
            message="pointer clicked",
            data={
                "x": resolved.point.x,
                "y": resolved.point.y,
                "button": button.value,
                "double": double,
                "matched": resolved.matched,
                "observation_id": observation.id,
            },
        )
        return resolved

    async def pointer_scroll(
        self,
        *,
        tenant_id: str,
        user_id: str,
        run_id: str,
        amount: int,
        target: GroundingTarget | None = None,
        observation_id: str | None = None,
    ) -> None:
        self._check_running(run_id)
        self._policy.check_scroll(amount)
        point = None
        if target is not None:
            observation = self._require_observation(run_id, observation_id)
            point = self.resolve(target, observation).point
        self._budget(run_id).consume("pointer", now=time.monotonic())
        await self._backend.pointer_scroll(amount=amount, point=point)
        await self._audit.record(
            AuditEventType.COMPUTER_SCROLLED,
            tenant_id=tenant_id,
            user_id=user_id,
            run_id=run_id,
            message="pointer scrolled",
            data={"amount": amount},
        )

    # -- Keyboard --------------------------------------------------------
    async def keyboard_type(
        self,
        *,
        tenant_id: str,
        user_id: str,
        run_id: str,
        text: str,
        observation_id: str | None = None,
    ) -> int:
        self._check_running(run_id)
        self._policy.check_text(text)
        if observation_id is not None:
            self._require_observation(run_id, observation_id)
        self._budget(run_id).consume("keyboard", now=time.monotonic())
        await self._backend.keyboard_type(text)
        await self._audit.record(
            AuditEventType.COMPUTER_TEXT_TYPED,
            tenant_id=tenant_id,
            user_id=user_id,
            run_id=run_id,
            message="text typed",
            # Length only — never the typed characters (may be a secret).
            data={"length": len(text)},
        )
        return len(text)

    async def keyboard_key(self, *, tenant_id: str, user_id: str, run_id: str, key: str) -> None:
        self._check_running(run_id)
        self._policy.check_keys([key])
        self._budget(run_id).consume("keyboard", now=time.monotonic())
        await self._backend.keyboard_key(key)
        await self._audit.record(
            AuditEventType.COMPUTER_KEY_PRESSED,
            tenant_id=tenant_id,
            user_id=user_id,
            run_id=run_id,
            message="key pressed",
            data={"key": key.lower()},
        )

    async def keyboard_hotkey(
        self, *, tenant_id: str, user_id: str, run_id: str, keys: list[str]
    ) -> None:
        self._check_running(run_id)
        self._policy.check_keys(keys)
        self._budget(run_id).consume("keyboard", now=time.monotonic())
        await self._backend.keyboard_hotkey(keys)
        await self._audit.record(
            AuditEventType.COMPUTER_KEY_PRESSED,
            tenant_id=tenant_id,
            user_id=user_id,
            run_id=run_id,
            message="hotkey pressed",
            data={"keys": [k.lower() for k in keys]},
        )

    # -- Windows ---------------------------------------------------------
    async def list_windows(self, *, run_id: str) -> list[WindowInfo]:
        self._check_running(run_id)
        if not self._backend.supports_windows:
            return []
        return await self._backend.list_windows()

    async def focus_window(
        self, *, tenant_id: str, user_id: str, run_id: str, window_id: str
    ) -> bool:
        self._check_running(run_id)
        if not self._backend.supports_windows:
            raise ValidationError("window control is not supported by this backend")
        focused = await self._backend.focus_window(window_id)
        event = (
            AuditEventType.COMPUTER_WINDOW_FOCUSED
            if focused
            else AuditEventType.COMPUTER_ACTION_FAILED
        )
        await self._audit.record(
            event,
            tenant_id=tenant_id,
            user_id=user_id,
            run_id=run_id,
            message="window focus requested",
            data={"window_id": window_id, "focused": focused},
        )
        return focused

    async def aclose(self) -> None:
        await self._backend.aclose()


__all__ = ["ComputerRuntime"]
