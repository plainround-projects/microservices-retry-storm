"""Health checks: liveness says the process runs; readiness says its dependencies answer."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

CheckFn = Callable[[], None]


@dataclass(frozen=True, slots=True)
class HealthReport:
    """The outcome of running every readiness check."""

    ready: bool
    checks: dict[str, str] = field(default_factory=dict)


class HealthRegistry:
    """Named readiness checks. A check raises when its dependency is unreachable."""

    def __init__(self) -> None:
        self._checks: dict[str, CheckFn] = {}

    def add(self, name: str, check: CheckFn) -> None:
        """Register a check under ``name``."""
        self._checks[name] = check

    def run(self) -> HealthReport:
        """Run every check; a raised exception marks that dependency as failed."""
        results: dict[str, str] = {}
        for name, check in self._checks.items():
            try:
                check()
                results[name] = "ok"
            except Exception as error:
                results[name] = f"failed: {type(error).__name__}"
        return HealthReport(ready=all(value == "ok" for value in results.values()), checks=results)
