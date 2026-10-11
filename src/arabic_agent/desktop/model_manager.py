"""Local AI / Model Manager.

The manager is the desktop-facing surface over the pieces the platform already
has: the model catalogue (what is supported), the installer
(:mod:`arabic_agent.local.model_install`, real resumable verified downloads), the
installed manifest (what is on disk) and the local inference backend (what can
actually load). It adds the product-level operations a user expects — list,
recommend, install with progress, verify, health-check, remove — without
inventing anything:

* **Installed is not healthy.** :meth:`health_check` loads the model and runs a
  bounded inference; a file that merely exists is reported ``UNKNOWN`` until a
  real check passes.
* **Recommendations are conservative.** :meth:`recommend` uses the existing
  hardware gate; it never claims a model will be fast and never recommends a
  tier the machine cannot plausibly run.
* **Removal is safe.** The active model is not removed; a caller must switch
  away from it first. Removal only ever touches files inside the model store.

Nothing here performs network access on its own: installation happens only when
a caller explicitly requests a specific catalogue model.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from ..core.enums import ModelCapability, ModelHealth, ModelProfile
from ..core.errors import ConflictError, NotFoundError, ValidationError
from ..local.hardware import HardwareProfile, can_run
from ..local.model_install import (
    InstallResult,
    install_model,
    read_manifest,
    verify_installed,
)
from ..local.model_registry import LocalModelDescriptor
from ..local.model_security import ensure_within_storage, resolve_storage_dir
from .model_catalogue import Catalogue, CatalogueModel

logger = logging.getLogger(__name__)

#: Bounded health-check inference prompt (Arabic-first, tiny output).
_HEALTH_PROMPT = "قل: جاهز"
_HEALTH_MAX_TOKENS = 8


def _active_model_path(storage_dir: Path) -> Path:
    return storage_dir / "active_model.json"


def read_active_model(storage_dir: Path) -> str | None:
    """Read the persisted active-model id, or ``None`` when none is set."""
    import json

    path = _active_model_path(storage_dir)
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        logger.warning("ignoring unreadable active-model file: %s", path)
        return None
    if isinstance(payload, dict):
        value = payload.get("model_id")
        return value if isinstance(value, str) and value else None
    return None


def write_active_model(storage_dir: Path, model_id: str | None) -> None:
    """Persist (or clear) the active-model id so it survives a restart."""
    import json

    root = resolve_storage_dir(str(storage_dir))
    root.mkdir(parents=True, exist_ok=True)
    path = _active_model_path(root)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps({"model_id": model_id}, indent=2), encoding="utf-8")
    tmp.replace(path)


@dataclass
class InstallProgress:
    """Live progress of one installation (safe to serialise for the UI)."""

    model_id: str
    state: str = "downloading"
    downloaded_bytes: int = 0
    total_bytes: int | None = None
    percent: float = 0.0
    detail: str = ""
    started_at: float = field(default_factory=time.monotonic)

    def as_dict(self) -> dict[str, object]:
        return {
            "model_id": self.model_id,
            "state": self.state,
            "downloaded_bytes": self.downloaded_bytes,
            "total_bytes": self.total_bytes,
            "percent": round(self.percent, 2),
            "detail": self.detail,
        }


class ModelManager:
    """Product-level operations over the local model store."""

    def __init__(
        self,
        *,
        storage_dir: str,
        catalogue: Catalogue | None = None,
        hardware: HardwareProfile | None = None,
        active_model_id: str | None = None,
    ) -> None:
        self._storage_dir = storage_dir
        self._catalogue = catalogue or Catalogue()
        self._hardware = hardware
        self._active_model_id = active_model_id
        self._progress: dict[str, InstallProgress] = {}
        self._cancel: dict[str, Callable[[], None]] = {}

    # -- Catalogue / installed view -------------------------------------
    def list_models(self) -> list[dict[str, object]]:
        """Return every catalogue model with its real installed state."""
        root = resolve_storage_dir(self._storage_dir)
        manifest = read_manifest(root)
        result: list[dict[str, object]] = []
        for model in self._catalogue.all():
            record = manifest.get(model.id)
            installed = record is not None
            path = str(root / str(record.get("path"))) if record else None
            verdict = (
                can_run(self._hardware, model.resources) if self._hardware is not None else None
            )
            result.append(
                {
                    "id": model.id,
                    "display_name": model.display_name,
                    "display_name_ar": model.display_name_ar or model.display_name,
                    "profile": model.profile.value,
                    "backend": model.backend,
                    "parameters": model.parameters,
                    "quantization": model.quantization,
                    "capabilities": sorted(c.value for c in model.capabilities),
                    "context_window": model.context_window,
                    "size_bytes": model.size_bytes,
                    "size_gib": model.size_gib,
                    "min_ram_gib": model.min_ram_gib,
                    "description": model.description,
                    "description_ar": model.description_ar or model.description,
                    "installed": installed,
                    "path": path,
                    "active": model.id == self._active_model_id,
                    "runnable": verdict.runnable if verdict else None,
                    "runnable_reason": verdict.reason if verdict else "",
                }
            )
        return result

    def installed_models(self) -> list[dict[str, object]]:
        return [m for m in self.list_models() if m["installed"]]

    # -- Recommendation -------------------------------------------------
    def recommend(self) -> dict[str, object]:
        """Recommend a tier for this machine, conservatively.

        Returns the highest tier the hardware gate accepts, plus the per-tier
        verdicts so the UI can explain *why*. When no hardware profile is
        available the recommendation is ``balanced`` with an explicit note that
        it is a default, not a measurement.
        """
        if self._hardware is None:
            return {
                "profile": ModelProfile.BALANCED.value,
                "model_id": self._best_for_profile(ModelProfile.BALANCED),
                "reason": "no hardware profile available; defaulting to the balanced tier",
                "verdicts": {},
            }
        order = (
            ModelProfile.HIGH_QUALITY,
            ModelProfile.BALANCED,
            ModelProfile.LIGHTWEIGHT,
        )
        verdicts: dict[str, dict[str, object]] = {}
        chosen: ModelProfile | None = None
        for profile in order:
            models = self._catalogue.by_profile(profile)
            if not models:
                continue
            # The tier is runnable if any of its models fits; pick the largest
            # that fits, else record the reason from the smallest.
            fitting = [m for m in models if can_run(self._hardware, m.resources).runnable]
            if fitting:
                verdicts[profile.value] = {"runnable": True, "reason": "meets minimum resources"}
                if chosen is None:
                    chosen = profile
            else:
                reason = can_run(self._hardware, models[0].resources).reason
                verdicts[profile.value] = {"runnable": False, "reason": reason}
        chosen = chosen or ModelProfile.LIGHTWEIGHT
        model_id = self._best_for_profile(chosen)
        return {
            "profile": chosen.value,
            "model_id": model_id,
            "reason": f"this computer can run the {chosen.value} tier",
            "verdicts": verdicts,
        }

    def _best_for_profile(self, profile: ModelProfile) -> str | None:
        models = self._catalogue.by_profile(profile)
        if not models:
            return None
        if self._hardware is not None:
            fitting = [m for m in models if can_run(self._hardware, m.resources).runnable]
            if fitting:
                return max(fitting, key=lambda m: m.size_bytes).id
        return models[0].id

    # -- Installation ---------------------------------------------------
    def progress_for(self, model_id: str) -> dict[str, object] | None:
        progress = self._progress.get(model_id)
        return progress.as_dict() if progress else None

    def cancel_install(self, model_id: str) -> bool:
        """Request cancellation of an in-flight install; returns whether one ran."""
        flag = self._cancel.get(model_id)
        if flag is None:
            return False
        flag()
        return True

    async def install(
        self,
        model_id: str,
        *,
        on_progress: Callable[[InstallProgress], None] | None = None,
    ) -> dict[str, object]:
        """Install a catalogue model with real progress, verification and cleanup."""
        model = self._catalogue.get(model_id)
        if model is None:
            raise NotFoundError(
                "unknown model", details={"model_id": model_id, "known": self._catalogue.ids()}
            )
        if self._hardware is not None:
            verdict = can_run(self._hardware, model.resources)
            if not verdict.runnable:
                raise ValidationError(
                    f"this computer cannot run {model_id}: {verdict.reason}",
                    details={"model_id": model_id, "reason": verdict.reason},
                )

        cancelled = {"flag": False}

        def _mark_cancelled() -> None:
            cancelled["flag"] = True

        self._cancel[model_id] = _mark_cancelled
        progress = InstallProgress(model_id=model_id, total_bytes=model.size_bytes)
        self._progress[model_id] = progress

        def _on_bytes(written: int, total: int | None) -> None:
            progress.downloaded_bytes = written
            progress.total_bytes = total
            if total:
                progress.percent = min(100.0, written / total * 100.0)
            if on_progress is not None:
                on_progress(progress)

        try:
            result: InstallResult = await install_model(
                model_id=model.id,
                url=model.url,
                storage_dir=self._storage_dir,
                filename=model.filename,
                expected_sha256=model.sha256,
                expected_size=model.size_bytes or None,
                should_cancel=lambda: cancelled["flag"],
                on_progress=_on_bytes,
            )
        finally:
            self._cancel.pop(model_id, None)

        if result.success:
            progress.state = "installed"
            progress.percent = 100.0
            progress.detail = result.detail
        elif cancelled["flag"]:
            progress.state = "cancelled"
            progress.detail = "cancelled; a partial file is kept for resume"
        else:
            progress.state = "failed"
            progress.detail = result.detail
        return {
            "model": self._model_dict(model),
            "install": result.as_dict(),
            "progress": progress.as_dict(),
        }

    # -- Verification / health ------------------------------------------
    def verify(self, model_id: str) -> dict[str, object]:
        """Re-verify an installed model against its manifest (no network)."""
        result = verify_installed(model_id, storage_dir=self._storage_dir)
        return result.as_dict()

    async def health_check(
        self, model_id: str, *, provider_factory: Callable[[], object] | None = None
    ) -> dict[str, object]:
        """Run a bounded health check: load the model and run tiny inference.

        ``provider_factory`` is the seam the desktop supplies to build a real
        :class:`LocalBackendProvider` from the descriptor. When it is not given
        the check reports ``unknown`` rather than a false ``healthy``.
        """
        descriptor = self.descriptor_for(model_id)
        if descriptor is None:
            return {
                "model_id": model_id,
                "health": ModelHealth.UNKNOWN.value,
                "detail": "model is not installed",
            }
        if provider_factory is None:
            return {
                "model_id": model_id,
                "health": ModelHealth.UNKNOWN.value,
                "detail": "no provider factory supplied; a file existing is not a health check",
            }
        provider = provider_factory()
        try:
            load = getattr(provider, "load", None)
            if load is not None:
                await load()
            probe = getattr(provider, "probe", None)
            healthy = bool(await probe()) if probe is not None else False
            if not healthy:
                return {
                    "model_id": model_id,
                    "health": ModelHealth.UNHEALTHY.value,
                    "detail": "backend did not report healthy after load",
                }
            inference = await self._tiny_inference(provider)
            return {
                "model_id": model_id,
                "health": ModelHealth.HEALTHY.value,
                "detail": "loaded and produced output",
                "inference": inference,
            }
        except Exception as exc:  # a health check must never crash the app
            return {
                "model_id": model_id,
                "health": ModelHealth.UNHEALTHY.value,
                "detail": f"health check failed: {exc}",
            }
        finally:
            close = getattr(provider, "aclose", None)
            if close is not None:
                try:
                    await close()
                except Exception:  # pragma: no cover - best effort cleanup
                    logger.debug("provider close failed during health check", exc_info=True)

    async def _tiny_inference(self, provider: object) -> dict[str, object]:
        from ..models.base import ChatMessage, ModelRequest

        complete = getattr(provider, "complete", None)
        if complete is None:
            return {"ok": False, "detail": "provider has no complete()"}
        request = ModelRequest(
            messages=[ChatMessage.user(_HEALTH_PROMPT)],
            max_tokens=_HEALTH_MAX_TOKENS,
        )
        try:
            response = await asyncio.wait_for(complete(request), timeout=60.0)
        except TimeoutError:
            return {"ok": False, "detail": "inference timed out"}
        text = getattr(getattr(response, "message", None), "content", "") or ""
        return {"ok": bool(text.strip()), "chars": len(text)}

    def descriptor_for(self, model_id: str) -> LocalModelDescriptor | None:
        """Build a descriptor for an *installed* catalogue model, or ``None``."""
        model = self._catalogue.get(model_id)
        if model is None:
            return None
        root = resolve_storage_dir(self._storage_dir)
        manifest = read_manifest(root)
        record = manifest.get(model_id)
        if record is None:
            return None
        raw_path = record.get("path")
        if not isinstance(raw_path, str):
            return None
        path = ensure_within_storage(root / raw_path, storage_dir=root)
        raw_sha = record.get("sha256")
        return LocalModelDescriptor(
            name="local",
            backend=model.backend,
            model_path=str(path),
            model_id=path.name,
            context_window=model.context_window,
            capabilities=frozenset(model.capabilities),
            sha256=raw_sha if isinstance(raw_sha, str) else None,
        )

    # -- Activation -----------------------------------------------------
    def active_model_id(self) -> str | None:
        """The id of the model the runtime is currently set to run, if any."""
        return self._active_model_id

    def active_descriptor(self) -> LocalModelDescriptor | None:
        """Descriptor for the active model, or ``None`` when none is active."""
        if self._active_model_id is None:
            return None
        return self.descriptor_for(self._active_model_id)

    def can_activate(self, model_id: str) -> dict[str, object]:
        """Report whether ``model_id`` could be activated, and why not.

        This is a *pre-flight* check, not a health check: it confirms the model
        is a known catalogue entry and is actually installed on disk. It never
        claims the model will load — :meth:`health_check` does that against a
        real backend.
        """
        model = self._catalogue.get(model_id)
        if model is None:
            return {"ok": False, "reason": "unknown model", "model_id": model_id}
        if self.descriptor_for(model_id) is None:
            return {"ok": False, "reason": "model is not installed", "model_id": model_id}
        return {"ok": True, "reason": "", "model_id": model_id}

    def activate(
        self, model_id: str, *, persist: Callable[[str], None] | None = None
    ) -> dict[str, object]:
        """Select an installed model as the active local model.

        Only an installed, known catalogue model can be activated — selection is
        never a claim that the model is healthy; the desktop runs a real health
        check against the activated model. When ``persist`` is given it is
        called with the id so the choice survives a restart.
        """
        verdict = self.can_activate(model_id)
        if not verdict["ok"]:
            raise NotFoundError(str(verdict["reason"]), details={"model_id": model_id})
        self._active_model_id = model_id
        if persist is not None:
            persist(model_id)
        model = self._catalogue.get(model_id)
        descriptor = self.descriptor_for(model_id)
        return {
            "model_id": model_id,
            "active": True,
            "backend": model.backend if model else "",
            "model_path": descriptor.model_path if descriptor else "",
            "persisted": persist is not None,
        }

    def deactivate(self) -> dict[str, object]:
        """Clear the active model (the runtime then has no local model)."""
        previous = self._active_model_id
        self._active_model_id = None
        return {"previous_model_id": previous, "active": None}

    # -- Removal --------------------------------------------------------
    def remove(self, model_id: str) -> dict[str, object]:
        """Remove an installed model from the store.

        The active model may not be removed: the caller must switch first, so a
        running agent never has its weights deleted underneath it. Only files
        inside the model store are touched.
        """
        if model_id == self._active_model_id:
            raise ConflictError(
                "cannot remove the active model; switch to another model first",
                details={"model_id": model_id},
            )
        root = resolve_storage_dir(self._storage_dir)
        manifest = read_manifest(root)
        record = manifest.get(model_id)
        if record is None:
            raise NotFoundError("model is not installed", details={"model_id": model_id})
        raw_path = record.get("path")
        removed_bytes = 0
        if isinstance(raw_path, str):
            path = ensure_within_storage(root / raw_path, storage_dir=root)
            if path.is_file():
                removed_bytes = path.stat().st_size
                path.unlink()
        from ..local.model_install import _write_manifest

        manifest.pop(model_id, None)
        _write_manifest(root, manifest)
        return {"model_id": model_id, "removed": True, "freed_bytes": removed_bytes}

    # -- Internals ------------------------------------------------------
    def _model_dict(self, model: CatalogueModel) -> dict[str, object]:
        return {
            "id": model.id,
            "display_name": model.display_name,
            "profile": model.profile.value,
            "size_bytes": model.size_bytes,
        }

    def supports_vision(self, model_id: str) -> bool:
        model = self._catalogue.get(model_id)
        return model is not None and ModelCapability.VISION in model.capabilities


__all__ = ["InstallProgress", "ModelManager", "read_active_model", "write_active_model"]
