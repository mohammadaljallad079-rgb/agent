"""Local capability model and registry.

A *capability* is the declarative description of something the local runtime
can do on the user's machine: its identity, the permissions it requires, its
risk class, which platforms support it, whether it works offline, whether it
needs human approval, and what it must audit.

The capability registry is metadata only — it does **not** execute anything.
Execution stays in the existing tool/executor path. The registry exists so the
local control API can honestly advertise what is available, so policy can be
reasoned about without importing every tool, and so the runtime can refuse to
advertise a capability that is not actually implemented on this platform.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from ..core.enums import CapabilityRisk, DataBoundary, PlatformKind, RiskLevel
from ..core.errors import NotFoundError
from ..permissions.base import Permission

#: Map a capability risk class onto the platform's coarse :class:`RiskLevel`.
_CAPABILITY_TO_RISK: dict[CapabilityRisk, RiskLevel] = {
    CapabilityRisk.READ_ONLY: RiskLevel.LOW,
    CapabilityRisk.LOW_RISK: RiskLevel.LOW,
    CapabilityRisk.MODERATE_RISK: RiskLevel.MEDIUM,
    CapabilityRisk.HIGH_RISK: RiskLevel.HIGH,
    CapabilityRisk.CRITICAL: RiskLevel.CRITICAL,
}


class LocalCapability(BaseModel):
    """Declarative description of one local capability."""

    model_config = ConfigDict(frozen=True)

    #: Stable identifier, e.g. ``files.read``.
    id: str
    #: Human-readable name (English; localization is a UI concern).
    name: str
    description: str
    #: The tool that implements this capability, if it is implemented.
    tool_name: str | None = None
    #: Risk class of the capability.
    risk: CapabilityRisk = CapabilityRisk.MODERATE_RISK
    #: Permissions a caller must hold to use the capability.
    permissions: tuple[Permission, ...] = ()
    #: Platforms on which the capability is available. Empty = all platforms.
    platforms: tuple[PlatformKind, ...] = ()
    #: Whether the capability works with no network connectivity.
    available_offline: bool = True
    #: Whether the capability inherently requires network access.
    requires_network: bool = False
    #: Whether the data this capability touches must stay local.
    data_boundary: DataBoundary = DataBoundary.LOCAL
    #: Whether human approval is always required (in addition to policy).
    requires_approval: bool = False
    #: Whether the capability is actually implemented in this build.
    implemented: bool = True
    #: Audit events this capability emits.
    audit_events: tuple[str, ...] = ()

    @property
    def risk_level(self) -> RiskLevel:
        return _CAPABILITY_TO_RISK[self.risk]

    def supports_platform(self, platform: PlatformKind) -> bool:
        return not self.platforms or platform in self.platforms

    def available(self, *, platform: PlatformKind, online: bool) -> bool:
        """Whether this capability can run right now."""
        if not self.implemented:
            return False
        if not self.supports_platform(platform):
            return False
        return not (self.requires_network and not online)


def default_local_capabilities(
    *, computer_control: bool = False, browser: bool = False
) -> tuple[LocalCapability, ...]:
    """The capability catalogue for this build.

    Only the safe foundational filesystem capabilities are marked
    ``implemented=True``. The higher-risk computer-control capabilities are
    declared with ``implemented=False`` so the API and audit surface exist, the
    policy vocabulary is stable, and nothing pretends to work. When the operator
    explicitly enables computer control (a local runtime only), screen capture
    and synthetic input become implemented, since the real tools are registered.
    Likewise, the browser capability becomes implemented only when browsing is
    explicitly enabled and a backend is available.
    """
    return (
        LocalCapability(
            id="files.read",
            name="Read file",
            description="Read a UTF-8 text file from an allowed directory.",
            tool_name="read_local_file",
            risk=CapabilityRisk.READ_ONLY,
            permissions=(Permission.FILES_READ,),
            data_boundary=DataBoundary.LOCAL,
            audit_events=("local.tool.requested", "local.file.accessed"),
        ),
        LocalCapability(
            id="files.list",
            name="List directory",
            description="List the entries of an allowed directory.",
            tool_name="list_local_directory",
            risk=CapabilityRisk.READ_ONLY,
            permissions=(Permission.FILES_READ,),
            data_boundary=DataBoundary.LOCAL,
            audit_events=("local.tool.requested", "local.file.accessed"),
        ),
        LocalCapability(
            id="files.search",
            name="Search files",
            description="Search for files by name/pattern within an allowed directory.",
            tool_name="search_local_files",
            risk=CapabilityRisk.READ_ONLY,
            permissions=(Permission.FILES_SEARCH,),
            data_boundary=DataBoundary.LOCAL,
            audit_events=("local.tool.requested", "local.file.accessed"),
        ),
        LocalCapability(
            id="files.write",
            name="Write file",
            description="Write UTF-8 text to a file inside an allowed directory.",
            tool_name="write_local_file",
            risk=CapabilityRisk.MODERATE_RISK,
            permissions=(Permission.FILES_WRITE,),
            requires_approval=True,
            data_boundary=DataBoundary.LOCAL,
            audit_events=("local.tool.requested", "local.file.modified"),
        ),
        LocalCapability(
            id="files.copy",
            name="Copy file",
            description="Copy a file within allowed directories.",
            tool_name="copy_local_file",
            risk=CapabilityRisk.MODERATE_RISK,
            permissions=(Permission.FILES_WRITE,),
            requires_approval=True,
            data_boundary=DataBoundary.LOCAL,
            audit_events=("local.tool.requested", "local.file.modified"),
        ),
        LocalCapability(
            id="files.move",
            name="Move / rename file",
            description="Move or rename a file within allowed directories.",
            tool_name="move_local_file",
            risk=CapabilityRisk.MODERATE_RISK,
            permissions=(Permission.FILES_MOVE,),
            requires_approval=True,
            data_boundary=DataBoundary.LOCAL,
            audit_events=("local.tool.requested", "local.file.modified"),
        ),
        LocalCapability(
            id="files.mkdir",
            name="Create directory",
            description="Create a directory inside an allowed directory.",
            tool_name="create_local_directory",
            risk=CapabilityRisk.MODERATE_RISK,
            permissions=(Permission.FILES_WRITE,),
            requires_approval=True,
            data_boundary=DataBoundary.LOCAL,
            audit_events=("local.tool.requested", "local.file.modified"),
        ),
        LocalCapability(
            id="files.delete",
            name="Delete file",
            description="Delete a file inside an allowed directory (denied by default).",
            tool_name="delete_local_file",
            risk=CapabilityRisk.HIGH_RISK,
            permissions=(Permission.FILES_DELETE,),
            requires_approval=True,
            data_boundary=DataBoundary.LOCAL,
            audit_events=("local.tool.requested", "local.file.modified"),
        ),
        # --- Phase 6: file intelligence, documents and project understanding -
        LocalCapability(
            id="files.read_range",
            name="Read file range",
            description="Read a bounded line range from a file.",
            tool_name="read_file_range",
            risk=CapabilityRisk.READ_ONLY,
            permissions=(Permission.READ_FILE,),
            data_boundary=DataBoundary.LOCAL,
            audit_events=("local.file.accessed",),
        ),
        LocalCapability(
            id="files.edit",
            name="Edit file",
            description="Edit a text file by exact replacement.",
            tool_name="edit_file",
            risk=CapabilityRisk.MODERATE_RISK,
            permissions=(Permission.WRITE_FILE,),
            requires_approval=True,
            data_boundary=DataBoundary.LOCAL,
            audit_events=("local.file.modified",),
        ),
        LocalCapability(
            id="files.info",
            name="File metadata",
            description="Return bounded metadata and a hash for a path.",
            tool_name="file_info",
            risk=CapabilityRisk.READ_ONLY,
            permissions=(Permission.READ_FILE,),
            data_boundary=DataBoundary.LOCAL,
            audit_events=("local.file.accessed",),
        ),
        LocalCapability(
            id="files.tree",
            name="Directory tree",
            description="Return a bounded directory tree.",
            tool_name="directory_tree",
            risk=CapabilityRisk.READ_ONLY,
            permissions=(Permission.LIST_DIRECTORY,),
            data_boundary=DataBoundary.LOCAL,
            audit_events=("local.file.accessed",),
        ),
        LocalCapability(
            id="files.search_contents",
            name="Search file contents",
            description="Search files by name or content within a directory.",
            tool_name="search_files",
            risk=CapabilityRisk.READ_ONLY,
            permissions=(Permission.LIST_DIRECTORY,),
            data_boundary=DataBoundary.LOCAL,
            audit_events=("local.file.accessed",),
        ),
        LocalCapability(
            id="documents.inspect",
            name="Inspect document",
            description="Inspect a document's structure and provenance without reading it all.",
            tool_name="inspect_document",
            risk=CapabilityRisk.READ_ONLY,
            permissions=(Permission.FILES_READ,),
            data_boundary=DataBoundary.LOCAL,
            audit_events=("document.parsed",),
        ),
        LocalCapability(
            id="documents.read",
            name="Read document",
            description="Read a bounded, provenance-bearing portion of a document.",
            tool_name="read_document",
            risk=CapabilityRisk.READ_ONLY,
            permissions=(Permission.FILES_READ,),
            data_boundary=DataBoundary.LOCAL,
            audit_events=("document.parsed",),
        ),
        LocalCapability(
            id="documents.search",
            name="Search document",
            description="Retrieve the most relevant sections of a document with provenance.",
            tool_name="search_document",
            risk=CapabilityRisk.READ_ONLY,
            permissions=(Permission.FILES_READ,),
            data_boundary=DataBoundary.LOCAL,
            audit_events=("document.retrieved",),
        ),
        LocalCapability(
            id="documents.list",
            name="List documents",
            description="List readable documents under a directory.",
            tool_name="list_documents",
            risk=CapabilityRisk.READ_ONLY,
            permissions=(Permission.FILES_SEARCH,),
            data_boundary=DataBoundary.LOCAL,
            audit_events=("document.retrieved",),
        ),
        LocalCapability(
            id="archives.inspect",
            name="Inspect archive",
            description="Safely list ZIP/TAR members without extracting or executing.",
            tool_name="inspect_archive",
            risk=CapabilityRisk.READ_ONLY,
            permissions=(Permission.FILES_READ,),
            data_boundary=DataBoundary.LOCAL,
            audit_events=("document.parsed",),
        ),
        LocalCapability(
            id="documents.generate",
            name="Generate document",
            description=(
                "Create a real PDF/DOCX/XLSX/CSV/Markdown/HTML/JSON file from content "
                "blocks. Arabic is supported."
            ),
            tool_name="generate_document",
            risk=CapabilityRisk.MODERATE_RISK,
            permissions=(Permission.WRITE_FILE,),
            requires_approval=True,
            data_boundary=DataBoundary.LOCAL,
            audit_events=("document.generated", "local.file.modified"),
        ),
        LocalCapability(
            id="spreadsheets.generate",
            name="Generate spreadsheet",
            description="Create a real XLSX or CSV file from tabular data.",
            tool_name="create_spreadsheet",
            risk=CapabilityRisk.MODERATE_RISK,
            permissions=(Permission.WRITE_FILE,),
            requires_approval=True,
            data_boundary=DataBoundary.LOCAL,
            audit_events=("document.generated", "local.file.modified"),
        ),
        LocalCapability(
            id="project.inspect",
            name="Inspect project",
            description="Detect languages, manifests, tests, entrypoints and docs of a project.",
            tool_name="inspect_project",
            risk=CapabilityRisk.READ_ONLY,
            permissions=(Permission.FILES_READ, Permission.FILES_SEARCH),
            data_boundary=DataBoundary.LOCAL,
            audit_events=("project.inspected",),
        ),
        # --- Declared but NOT implemented (no fake functionality) -----------
        # Computer control becomes implemented only when the operator enables it
        # (see ``computer_control``); the vocabulary is declared either way.
        LocalCapability(
            id="clipboard.read",
            name="Read clipboard",
            description="Read the system clipboard.",
            risk=CapabilityRisk.MODERATE_RISK,
            permissions=(Permission.CLIPBOARD_READ,),
            requires_approval=True,
            data_boundary=DataBoundary.LOCAL,
            implemented=False,
        ),
        LocalCapability(
            id="clipboard.write",
            name="Write clipboard",
            description="Write text to the system clipboard.",
            risk=CapabilityRisk.MODERATE_RISK,
            permissions=(Permission.CLIPBOARD_WRITE,),
            requires_approval=True,
            data_boundary=DataBoundary.LOCAL,
            implemented=False,
        ),
        LocalCapability(
            id="screen.capture",
            name="Capture screen",
            description="Capture a screenshot of the display and perceive its elements.",
            tool_name="computer_screen",
            risk=CapabilityRisk.HIGH_RISK,
            permissions=(Permission.SCREEN_READ,),
            requires_approval=True,
            data_boundary=DataBoundary.LOCAL,
            implemented=computer_control,
            audit_events=(
                "computer.screen_captured",
                "computer.boundary_decision",
            ),
        ),
        LocalCapability(
            id="input.control",
            name="Control keyboard / mouse",
            description="Synthesize keyboard and mouse input.",
            risk=CapabilityRisk.CRITICAL,
            permissions=(Permission.INPUT_CONTROL,),
            requires_approval=True,
            data_boundary=DataBoundary.LOCAL,
            implemented=False,
        ),
        LocalCapability(
            id="input.pointer",
            name="Control pointer",
            description="Move, click and scroll the pointer.",
            tool_name="computer_pointer",
            risk=CapabilityRisk.HIGH_RISK,
            permissions=(Permission.POINTER_CONTROL,),
            requires_approval=True,
            data_boundary=DataBoundary.LOCAL,
            implemented=computer_control,
            audit_events=("computer.pointer_moved", "computer.clicked", "computer.scrolled"),
        ),
        LocalCapability(
            id="input.keyboard",
            name="Control keyboard",
            description="Type text and press keys on the user's machine.",
            tool_name="computer_keyboard",
            risk=CapabilityRisk.HIGH_RISK,
            permissions=(Permission.KEYBOARD_CONTROL,),
            requires_approval=True,
            data_boundary=DataBoundary.LOCAL,
            implemented=computer_control,
            audit_events=("computer.text_typed", "computer.key_pressed"),
        ),
        LocalCapability(
            id="window.control",
            name="Control windows",
            description="List and focus windows on the user's display.",
            tool_name="computer_window",
            risk=CapabilityRisk.MODERATE_RISK,
            permissions=(Permission.WINDOW_CONTROL,),
            requires_approval=True,
            data_boundary=DataBoundary.LOCAL,
            implemented=computer_control,
            audit_events=("computer.window_focused",),
        ),
        LocalCapability(
            id="application.launch",
            name="Launch application",
            description="Start a local application.",
            risk=CapabilityRisk.HIGH_RISK,
            permissions=(Permission.APPLICATION_LAUNCH,),
            requires_approval=True,
            data_boundary=DataBoundary.LOCAL,
            implemented=False,
        ),
        LocalCapability(
            id="application.close",
            name="Close application",
            description="Stop a running local application.",
            risk=CapabilityRisk.HIGH_RISK,
            permissions=(Permission.APPLICATION_CLOSE,),
            requires_approval=True,
            data_boundary=DataBoundary.LOCAL,
            implemented=False,
        ),
        LocalCapability(
            id="print.document",
            name="Print document",
            description="Send a document to a printer.",
            risk=CapabilityRisk.MODERATE_RISK,
            permissions=(Permission.PRINT,),
            requires_approval=True,
            data_boundary=DataBoundary.LOCAL,
            implemented=False,
        ),
        LocalCapability(
            id="browser.control",
            name="Control browser",
            description="Drive a local web browser through the browser tools.",
            tool_name="browser_navigate",
            risk=CapabilityRisk.HIGH_RISK,
            permissions=(
                Permission.BROWSER_NAVIGATE,
                Permission.BROWSER_READ,
                Permission.BROWSER_INTERACT,
            ),
            requires_network=True,
            requires_approval=True,
            data_boundary=DataBoundary.LOCAL,
            audit_events=("browser.navigated", "browser.observed", "browser.interacted"),
            implemented=browser,
        ),
        LocalCapability(
            id="command.execute",
            name="Run command",
            description="Run a controlled local command (allow-listed, sandboxed).",
            tool_name="run_command",
            risk=CapabilityRisk.HIGH_RISK,
            permissions=(Permission.RUN_COMMAND,),
            requires_approval=True,
            data_boundary=DataBoundary.LOCAL,
            audit_events=("local.command.attempted",),
        ),
    )


class LocalCapabilityRegistry:
    """An immutable, queryable catalogue of local capabilities."""

    def __init__(
        self,
        capabilities: tuple[LocalCapability, ...] | None = None,
        *,
        computer_control: bool = False,
        browser: bool = False,
    ) -> None:
        items = (
            capabilities
            if capabilities is not None
            else default_local_capabilities(computer_control=computer_control, browser=browser)
        )
        self._by_id: dict[str, LocalCapability] = {}
        for capability in items:
            if capability.id in self._by_id:
                raise ValueError(f"duplicate local capability id: {capability.id}")
            self._by_id[capability.id] = capability

    def get(self, capability_id: str) -> LocalCapability:
        try:
            return self._by_id[capability_id]
        except KeyError as exc:
            raise NotFoundError(
                f"unknown local capability: {capability_id}",
                details={"known": self.ids()},
            ) from exc

    def has(self, capability_id: str) -> bool:
        return capability_id in self._by_id

    def ids(self) -> list[str]:
        return sorted(self._by_id)

    def all(self) -> tuple[LocalCapability, ...]:
        return tuple(self._by_id[i] for i in self.ids())

    def implemented(self) -> tuple[LocalCapability, ...]:
        return tuple(c for c in self.all() if c.implemented)

    def available(self, *, platform: PlatformKind, online: bool) -> tuple[LocalCapability, ...]:
        return tuple(c for c in self.all() if c.available(platform=platform, online=online))

    def for_tool(self, tool_name: str) -> tuple[LocalCapability, ...]:
        return tuple(c for c in self.all() if c.tool_name == tool_name)
