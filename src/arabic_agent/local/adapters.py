"""Cross-platform adapters.

Operating-system-specific functionality lives behind these adapters so the agent
logic is written once and never duplicated per OS. Each adapter declares whether
it is *available* on the current platform and, if not, *why* — an unavailable
adapter raises a clear error instead of pretending to work.

In this phase only the filesystem and process adapters are implemented (they are
the safe foundation the filesystem tools and the controlled command runner
need). Clipboard, printer, screen and input adapters are declared with
``available=False`` and a reason; wiring them is a later phase and is explicitly
listed as not implemented in the docs.
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

from ..core.enums import PlatformKind
from ..core.errors import ConfigurationError


def detect_platform() -> PlatformKind:
    """Return the current OS family."""
    if sys.platform.startswith("linux"):
        return PlatformKind.LINUX
    if sys.platform == "darwin":
        return PlatformKind.MACOS
    if sys.platform.startswith("win"):
        return PlatformKind.WINDOWS
    return PlatformKind.UNKNOWN


class Adapter:
    """Base class for platform adapters."""

    #: Whether this adapter can run on the current platform.
    available: bool = False
    #: Human-readable reason when :attr:`available` is ``False``.
    unavailable_reason: str = "not implemented"

    def require_available(self) -> None:
        if not self.available:
            raise ConfigurationError(
                f"{type(self).__name__} is unavailable: {self.unavailable_reason}"
            )


class FilesystemAdapter(Adapter):
    """Real filesystem operations. Platform-independent by design."""

    available = True

    def exists(self, path: Path) -> bool:
        return path.exists()

    def is_dir(self, path: Path) -> bool:
        return path.is_dir()

    def is_file(self, path: Path) -> bool:
        return path.is_file()

    def is_symlink(self, path: Path) -> bool:
        return path.is_symlink()

    def stat_size(self, path: Path) -> int:
        return path.stat().st_size

    def mkdir(self, path: Path, *, parents: bool = True) -> None:
        path.mkdir(parents=parents, exist_ok=True)

    def listdir(self, path: Path) -> list[Path]:
        return sorted(path.iterdir(), key=lambda p: p.name)

    def read_text(self, path: Path, *, max_bytes: int | None = None) -> tuple[str, bool]:
        """Read UTF-8 text; return ``(text, truncated)``."""
        raw = path.read_bytes()
        if max_bytes is not None and len(raw) > max_bytes:
            raw = raw[:max_bytes]
            return raw.decode("utf-8", errors="strict"), True
        return raw.decode("utf-8", errors="strict"), False

    def write_text(self, path: Path, content: str, *, append: bool = False) -> int:
        path.parent.mkdir(parents=True, exist_ok=True)
        mode = "a" if append else "w"
        with path.open(mode, encoding="utf-8") as handle:
            handle.write(content)
        return len(content.encode("utf-8"))

    def copy(self, source: Path, destination: Path) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)

    def move(self, source: Path, destination: Path) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(source), str(destination))

    def delete(self, path: Path) -> None:
        if path.is_dir():
            path.rmdir()
        else:
            path.unlink()


class ProcessAdapter(Adapter):
    """Real, controlled process execution.

    The adapter only *spawns*; allow-listing, working-directory restriction,
    environment filtering and output limits are enforced by the command runner
    that uses it. The adapter deliberately exposes a shell-free argv interface.
    """

    available = True

    def which(self, program: str) -> str | None:
        return shutil.which(program)

    def resolve_program(self, program: str) -> str | None:
        """Resolve a program name to an absolute path if it is on PATH."""
        if os.path.sep in program:
            return program if Path(program).exists() else None
        return shutil.which(program)


class ClipboardAdapter(Adapter):
    """Read/write the system clipboard. NOT implemented in this phase."""

    available = False
    unavailable_reason = "clipboard integration is not implemented in this phase"


class PrinterAdapter(Adapter):
    """Send documents to a printer. NOT implemented in this phase."""

    available = False
    unavailable_reason = "printer integration is not implemented in this phase"


class ScreenAdapter(Adapter):
    """Capture the screen. NOT implemented in this phase."""

    available = False
    unavailable_reason = "screen capture is not implemented in this phase"


class InputAdapter(Adapter):
    """Synthesize keyboard/mouse input. NOT implemented in this phase."""

    available = False
    unavailable_reason = "synthetic input control is not implemented in this phase"


class ApplicationAdapter(Adapter):
    """Launch/close local applications. NOT implemented in this phase."""

    available = False
    unavailable_reason = "application control is not implemented in this phase"


class PlatformAdapters:
    """The set of adapters for one runtime, chosen for the current platform."""

    def __init__(self, platform: PlatformKind | None = None) -> None:
        self.platform = platform or detect_platform()
        self.filesystem = FilesystemAdapter()
        self.process = ProcessAdapter()
        self.clipboard = ClipboardAdapter()
        self.printer = PrinterAdapter()
        self.screen = ScreenAdapter()
        self.input = InputAdapter()
        self.application = ApplicationAdapter()

    def all(self) -> tuple[Adapter, ...]:
        return (
            self.filesystem,
            self.process,
            self.clipboard,
            self.printer,
            self.screen,
            self.input,
            self.application,
        )

    def available_names(self) -> list[str]:
        return sorted(
            name
            for name in (
                "filesystem",
                "process",
                "clipboard",
                "printer",
                "screen",
                "input",
                "application",
            )
            if getattr(self, name).available
        )
