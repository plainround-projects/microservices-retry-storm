"""The faults currently injected; replaced as a whole when the experiment changes them."""

from __future__ import annotations

from stock.domain.models import Faults


class FaultState:
    """Holds the current :class:`Faults`."""

    def __init__(self, initial: Faults) -> None:
        self._faults = initial

    def get(self) -> Faults:
        """The faults in force now."""
        return self._faults

    def set(self, faults: Faults) -> None:
        """Replace the faults."""
        self._faults = faults
