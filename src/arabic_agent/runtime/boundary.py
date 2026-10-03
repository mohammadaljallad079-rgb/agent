"""Local/Cloud runtime boundaries.

The same agent core runs in two environments. This module names the boundary
explicitly so we never accidentally assume the cloud exists (offline) or that
the filesystem is private (cloud).

Cloud runtime:  web/desktop → API → agent core → cloud models + integrations
Local runtime:  desktop     → agent core → local tools + local files + local model
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from pydantic import BaseModel, ConfigDict

from ..core.enums import RuntimeKind, StrEnum


class Capability(StrEnum):
    """What a runtime can and cannot do, decided at the boundary."""

    LOCAL_FILESYSTEM = "local_filesystem"
    SHELL = "shell"
    LOCAL_MODEL = "local_model"
    CLOUD_MODEL = "cloud_model"
    EXTERNAL_INTEGRATIONS = "external_integrations"
    PERSISTENT_DATABASE = "persistent_database"
    MULTI_TENANT_ISOLATION = "multi_tenant_isolation"
    OFFLINE_OPERATION = "offline_operation"


class RuntimeProfile(BaseModel):
    """Declares which capabilities are available in a runtime."""

    model_config = ConfigDict(frozen=True)

    kind: RuntimeKind
    capabilities: frozenset[Capability]

    def supports(self, capability: Capability) -> bool:
        return capability in self.capabilities

    @classmethod
    def local(cls) -> RuntimeProfile:
        return cls(
            kind=RuntimeKind.LOCAL,
            capabilities=frozenset(
                {
                    Capability.LOCAL_FILESYSTEM,
                    Capability.SHELL,
                    Capability.LOCAL_MODEL,
                    Capability.OFFLINE_OPERATION,
                    Capability.CLOUD_MODEL,  # allowed when connectivity exists
                    Capability.EXTERNAL_INTEGRATIONS,
                }
            ),
        )

    @classmethod
    def cloud(cls) -> RuntimeProfile:
        return cls(
            kind=RuntimeKind.CLOUD,
            capabilities=frozenset(
                {
                    Capability.CLOUD_MODEL,
                    Capability.EXTERNAL_INTEGRATIONS,
                    Capability.PERSISTENT_DATABASE,
                    Capability.MULTI_TENANT_ISOLATION,
                }
            ),
        )


class Transport(ABC):
    """Contract for moving a run between local and cloud runtimes.

    Not implemented in this phase. Defining it now fixes the seam: a desktop
    agent will implement the client side, the cloud API the server side, and
    neither will leak into the agent core.
    """

    @abstractmethod
    async def submit_run(self, *, tenant_id: str, user_id: str, goal: str) -> str:
        """Submit a goal for execution; return a run identifier."""

    @abstractmethod
    async def poll_run(self, *, tenant_id: str, run_id: str) -> dict[str, object]:
        """Fetch the current state of a run."""
