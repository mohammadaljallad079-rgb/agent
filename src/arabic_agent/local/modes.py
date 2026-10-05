"""Operating-mode manager (online / offline / hybrid).

The runtime's mode is *declared*, not guessed from a failed request. This
manager holds the current mode and answers the questions other components need:

* May the runtime reach the cloud at all?
* Is a local-only operation still permitted? (always, in every mode)
* Should a boundary decision route a payload locally or remotely?

Mode changes are auditable; the caller records the audit event so the manager
itself stays free of the audit dependency.
"""

from __future__ import annotations

from ..core.enums import OperatingMode


class ModeManager:
    """Tracks the runtime's declared operating mode."""

    def __init__(self, mode: OperatingMode = OperatingMode.OFFLINE) -> None:
        self._mode = mode

    @property
    def mode(self) -> OperatingMode:
        return self._mode

    @property
    def online(self) -> bool:
        """Whether cloud reachability is permitted (ONLINE or HYBRID)."""
        return self._mode in (OperatingMode.ONLINE, OperatingMode.HYBRID)

    @property
    def offline(self) -> bool:
        return self._mode is OperatingMode.OFFLINE

    @property
    def hybrid(self) -> bool:
        return self._mode is OperatingMode.HYBRID

    def set_mode(self, mode: OperatingMode) -> OperatingMode:
        """Set the mode and return the previous one (for audit)."""
        previous = self._mode
        self._mode = mode
        return previous

    def may_use_cloud(self) -> bool:
        """Whether cloud models/workflows may be used in this mode."""
        return self.online

    def may_run_local(self) -> bool:
        """Local operation is always permitted, in every mode."""
        return True

    def requires_local_routing(self) -> bool:
        """Whether *all* processing must stay local (offline mode)."""
        return self._mode is OperatingMode.OFFLINE
