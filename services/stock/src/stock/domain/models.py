"""Value objects and errors of the stock store."""

from __future__ import annotations

from dataclasses import dataclass


class InjectedFailure(Exception):
    """A failure the experiment asked for (the configured fail rate)."""


class UnknownSku(LookupError):
    """No stock row exists for the sku."""

    def __init__(self, sku: str) -> None:
        super().__init__(f"unknown sku {sku!r}")
        self.sku = sku


class InvalidFaults(ValueError):
    """The requested latency or fail rate is out of range."""


@dataclass(frozen=True, slots=True)
class Faults:
    """What the experiment injects at the bottom of the chain: added latency and a share of failing calls."""

    latency_ms: int = 50
    fail_rate: float = 0.0

    def __post_init__(self) -> None:
        if not 0 <= self.latency_ms <= 60_000:
            raise InvalidFaults("latency_ms must be between 0 and 60000")
        if not 0.0 <= self.fail_rate <= 1.0:
            raise InvalidFaults("fail_rate must be between 0 and 1")


@dataclass(frozen=True, slots=True)
class StockAnswer:
    """Whether a sku is in stock, and how many units."""

    sku: str
    quantity: int

    @property
    def in_stock(self) -> bool:
        """True when at least one unit is left."""
        return self.quantity > 0
