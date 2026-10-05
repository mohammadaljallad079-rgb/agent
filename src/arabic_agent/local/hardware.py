"""Hardware and resource detection.

A local model runs on the user's own machine, so the runtime must be able to
answer two honest questions before it loads anything:

* *What does this machine have?* — CPU, RAM, GPU, free disk, architecture, OS.
* *Can this model plausibly run here?* — a conservative estimate, never a
  performance promise.

Detection reads only the local machine. It performs **no** network access and
never shells out to arbitrary commands; where a value cannot be determined it
reports ``None`` and says so rather than inventing a number.
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from ..core.enums import PlatformKind
from .adapters import detect_platform

#: Reserved head-room (bytes) that must remain free after a model is stored.
_DISK_HEADROOM_BYTES = 1 * 1024 * 1024 * 1024


class HardwareProfile(BaseModel):
    """A serialisable snapshot of the machine's resources."""

    model_config = ConfigDict(frozen=True)

    platform: PlatformKind
    architecture: str
    cpu_count: int
    cpu_model: str = ""
    total_ram_bytes: int | None = None
    available_ram_bytes: int | None = None
    disk_free_bytes: int | None = None
    gpu_available: bool = False
    gpu_names: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()

    @property
    def total_ram_gib(self) -> float | None:
        return None if self.total_ram_bytes is None else round(self.total_ram_bytes / 2**30, 2)

    @property
    def disk_free_gib(self) -> float | None:
        return None if self.disk_free_bytes is None else round(self.disk_free_bytes / 2**30, 2)


class ResourceRequirement(BaseModel):
    """Approximate resources a model needs to load and run.

    These are *estimates supplied by configuration or model metadata*, not
    measurements. :meth:`HardwareProfile.can_run` uses them to give a
    conservative yes/no, and never a speed claim.
    """

    model_config = ConfigDict(frozen=True)

    #: Minimum RAM (bytes) required to load the weights plus runtime overhead.
    min_ram_bytes: int = 0
    #: Disk space (bytes) the model file needs.
    disk_bytes: int = 0
    #: Whether the model benefits from (but does not require) a GPU.
    prefers_gpu: bool = False
    #: Whether the model requires a GPU (e.g. a CUDA-only build).
    requires_gpu: bool = False


def _cpu_model() -> str:
    """Best-effort CPU model name across Linux/macOS/Windows."""
    if sys.platform.startswith("linux"):
        try:
            for line in Path("/proc/cpuinfo").read_text(encoding="utf-8").splitlines():
                if line.lower().startswith("model name"):
                    return line.split(":", 1)[1].strip()
        except OSError:
            return ""
    if sys.platform == "darwin":
        return platform.processor() or ""
    return platform.processor() or ""


def _ram_bytes() -> tuple[int | None, int | None]:
    """Return ``(total, available)`` RAM in bytes, or ``(None, None)``.

    Uses only standard, cross-platform sources: ``os.sysconf`` on POSIX and
    ``GlobalMemoryStatusEx`` on Windows via ``ctypes`` (no third-party import).
    """
    if sys.platform.startswith("win"):  # pragma: no cover - exercised on Windows only
        try:
            import ctypes

            class _MemoryStatusEx(ctypes.Structure):
                _fields_ = [
                    ("dwLength", ctypes.c_ulong),
                    ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]

            status = _MemoryStatusEx()
            status.dwLength = ctypes.sizeof(_MemoryStatusEx)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):  # type: ignore[attr-defined]
                return int(status.ullTotalPhys), int(status.ullAvailPhys)
        except Exception:  # detection must never crash the runtime
            return None, None
        return None, None

    try:
        page = os.sysconf("SC_PAGE_SIZE")
        total = page * os.sysconf("SC_PHYS_PAGES")
        available = page * os.sysconf("SC_AVPHYS_PAGES")
        return int(total), int(available)
    except (ValueError, OSError, AttributeError):
        pass
    # macOS fallback: sysctl is part of the base system.
    if sys.platform == "darwin":
        try:
            out = subprocess.run(
                ["sysctl", "-n", "hw.memsize"],  # noqa: S607 - fixed system binary
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            if out.returncode == 0 and out.stdout.strip().isdigit():
                return int(out.stdout.strip()), None
        except (OSError, subprocess.SubprocessError):
            return None, None
    return None, None


def _gpu_names() -> tuple[str, ...]:
    """Detect GPUs via vendor CLIs if present. Read-only, best-effort."""
    names: list[str] = []
    nvidia = shutil.which("nvidia-smi")
    if nvidia:
        try:
            out = subprocess.run(  # noqa: S603 - resolved vendor binary, no shell
                [nvidia, "--query-gpu=name", "--format=csv,noheader"],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            if out.returncode == 0:
                names.extend(line.strip() for line in out.stdout.splitlines() if line.strip())
        except (OSError, subprocess.SubprocessError):
            pass
    return tuple(names)


def detect_hardware(*, disk_path: str | None = None) -> HardwareProfile:
    """Detect the current machine's resources.

    ``disk_path`` selects the filesystem whose free space is reported (the
    model storage directory); it defaults to the current working directory.
    """
    total_ram, available_ram = _ram_bytes()
    target = Path(disk_path).expanduser() if disk_path else Path.cwd()
    try:
        disk_free = shutil.disk_usage(target).free
    except OSError:
        disk_free = None

    notes: list[str] = []
    gpus = _gpu_names()
    if not gpus:
        notes.append("no supported GPU detected; inference will use the CPU")
    if total_ram is None:
        notes.append("total RAM could not be determined on this platform")

    return HardwareProfile(
        platform=detect_platform(),
        architecture=platform.machine() or "unknown",
        cpu_count=os.cpu_count() or 1,
        cpu_model=_cpu_model(),
        total_ram_bytes=total_ram,
        available_ram_bytes=available_ram,
        disk_free_bytes=disk_free,
        gpu_available=bool(gpus),
        gpu_names=gpus,
        notes=tuple(notes),
    )


class ResourceVerdict(BaseModel):
    """The answer to "can this model run here?" with the reason."""

    model_config = ConfigDict(frozen=True)

    runnable: bool
    reason: str


def can_run(profile: HardwareProfile, requirement: ResourceRequirement) -> ResourceVerdict:
    """Conservatively decide whether a model is likely runnable.

    This is a *gate*, not a benchmark: it refuses only when a hard minimum is
    clearly unmet, and it never claims the model will be fast. Unknown values
    (``None``) are treated as "cannot disprove", so an undetectable RAM figure
    does not block a load.
    """
    if requirement.requires_gpu and not profile.gpu_available:
        return ResourceVerdict(runnable=False, reason="model requires a GPU but none was detected")
    if (
        requirement.min_ram_bytes
        and profile.total_ram_bytes is not None
        and profile.total_ram_bytes < requirement.min_ram_bytes
    ):
        return ResourceVerdict(
            runnable=False,
            reason=(
                f"insufficient RAM: needs ~{requirement.min_ram_bytes / 2**30:.1f} GiB, "
                f"machine has {profile.total_ram_gib} GiB"
            ),
        )
    if (
        requirement.disk_bytes
        and profile.disk_free_bytes is not None
        and profile.disk_free_bytes < requirement.disk_bytes + _DISK_HEADROOM_BYTES
    ):
        needed = (requirement.disk_bytes + _DISK_HEADROOM_BYTES) / 2**30
        return ResourceVerdict(
            runnable=False,
            reason=(
                f"insufficient disk: needs ~{needed:.1f} GiB free, "
                f"machine has {profile.disk_free_gib} GiB"
            ),
        )
    return ResourceVerdict(runnable=True, reason="meets the minimum resource requirements")
