"""Local runtime package (Phase 4).

This package gives the *existing* agent core a secure execution bridge to the
user's machine. It deliberately adds no second agent loop, no second workflow
engine and no second permission system:

* the agent core stays the brain (``arabic_agent.agent``),
* the workflow engine stays the orchestrator (``arabic_agent.workflows``),
* the permission layer stays the authority (``arabic_agent.permissions``),
* this package is the *local transport and sandbox* those components run behind.

Nothing here is imported by the cloud runtime unless a local runtime is built,
and nothing here reaches around the policy gate: local tools are ordinary
:class:`~arabic_agent.tools.base.Tool` instances executed by the existing
:class:`~arabic_agent.agent.executor.ToolExecutor`.

This package's ``__init__`` is deliberately lightweight: it does **not** import
:mod:`arabic_agent.local.control` or :mod:`arabic_agent.local.http`, because
those reach the composition root and would create an import cycle with
``tools.builtin.local``. Import them directly where needed.
"""

from __future__ import annotations

from .adapters import PlatformAdapters, detect_platform
from .boundary import BoundaryDecision, DataBoundaryPolicy
from .capabilities import (
    LocalCapability,
    LocalCapabilityRegistry,
    default_local_capabilities,
)
from .device import DeviceIdentity, DeviceRegistry, InMemoryDeviceRegistry
from .lifecycle import LifecycleStateMachine
from .modes import ModeManager
from .runtime import LocalRuntime, LocalRuntimeStatus, build_local_runtime
from .sandbox import FilesystemPolicy, FilesystemSandbox

__all__ = [
    "BoundaryDecision",
    "DataBoundaryPolicy",
    "DeviceIdentity",
    "DeviceRegistry",
    "FilesystemPolicy",
    "FilesystemSandbox",
    "InMemoryDeviceRegistry",
    "LifecycleStateMachine",
    "LocalCapability",
    "LocalCapabilityRegistry",
    "LocalRuntime",
    "LocalRuntimeStatus",
    "ModeManager",
    "PlatformAdapters",
    "build_local_runtime",
    "default_local_capabilities",
    "detect_platform",
]
