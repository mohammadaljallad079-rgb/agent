"""First-run setup provider.

A freshly installed desktop product has no model yet: no cloud key is set and no
local weights are on disk. The gateway refuses to assemble with zero providers,
which would otherwise make it impossible to *open* the product in order to
configure a model — a real first-run deadlock.

This provider breaks that deadlock honestly. It is a real
:class:`~arabic_agent.models.base.ModelProvider` that is registered only when
nothing else is configured, so the platform can start and the UI can run its
model manager. It is *not* a mock: it performs no inference and always raises a
:class:`~arabic_agent.core.errors.ConfigurationError` naming the exact next step,
so a task can never silently "succeed" against it and no fake output is ever
produced. The desktop surfaces it as ``needs_setup`` rather than as a ready
provider.
"""

from __future__ import annotations

from ...core.enums import ModelCapability
from ...core.errors import ConfigurationError
from ..base import ModelProvider, ModelRequest, ModelResponse

#: The logical model id the gateway is configured with in setup mode.
SETUP_MODEL_ID = "setup/needs-model"

#: The message a caller sees when it tries to run before configuring a model.
_SETUP_MESSAGE = (
    "no model is configured yet: install and activate a local model, or set a "
    "cloud provider key, then try again"
)


class SetupRequiredProvider(ModelProvider):
    """A non-inference provider that reports the product still needs setup.

    Every call fails with an actionable :class:`ConfigurationError`. This is the
    deliberate opposite of a mock: it cannot fabricate a completion, and it
    makes the missing configuration impossible to overlook.
    """

    name = "setup"
    is_local = False
    capabilities = frozenset({ModelCapability.CHAT})

    def __init__(self, *, name: str = "setup", detail: str = _SETUP_MESSAGE) -> None:
        self.name = name
        self._detail = detail

    @property
    def detail(self) -> str:
        return self._detail

    async def complete(self, request: ModelRequest) -> ModelResponse:
        raise ConfigurationError(self._detail, details={"setup_required": True})


__all__ = ["SETUP_MODEL_ID", "SetupRequiredProvider"]
