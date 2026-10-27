"""Twelve-factor configuration: typed reads from an environment mapping with every problem reported at once."""

from __future__ import annotations

from collections.abc import Mapping

from chassis.errors import ConfigError


class EnvReader:
    """Reads typed values from a mapping and remembers every invalid or missing one.

    Call :meth:`finish` after reading everything: it raises one :class:`ConfigError` that lists all
    problems, so a misconfigured container fails once with a complete message instead of one variable at a time.
    """

    def __init__(self, environ: Mapping[str, str]) -> None:
        self._environ = environ
        self._problems: list[str] = []

    def text(self, name: str, default: str | None = None) -> str:
        """Return the variable, the default, or record that a required variable is missing."""
        value = self._environ.get(name)
        if value is None or value == "":
            if default is None:
                self._problems.append(f"{name} is required")
                return ""
            return default
        return value

    def integer(self, name: str, default: int, *, minimum: int = 0, maximum: int | None = None) -> int:
        """Return an integer within bounds, or the default when unset."""
        raw = self._environ.get(name)
        if raw is None or raw == "":
            return default
        try:
            value = int(raw)
        except ValueError:
            self._problems.append(f"{name} must be an integer, got {raw!r}")
            return default
        if value < minimum or (maximum is not None and value > maximum):
            self._problems.append(
                f"{name} must be between {minimum} and {maximum if maximum is not None else 'infinity'}"
            )
            return default
        return value

    def number(self, name: str, default: float, *, minimum: float = 0.0, maximum: float | None = None) -> float:
        """Return a float within bounds, or the default when unset."""
        raw = self._environ.get(name)
        if raw is None or raw == "":
            return default
        try:
            value = float(raw)
        except ValueError:
            self._problems.append(f"{name} must be a number, got {raw!r}")
            return default
        if value < minimum or (maximum is not None and value > maximum):
            self._problems.append(
                f"{name} must be between {minimum} and {maximum if maximum is not None else 'infinity'}"
            )
            return default
        return value

    def choice(self, name: str, default: str, allowed: tuple[str, ...]) -> str:
        """Return one of the allowed strings, or the default when unset."""
        value = self._environ.get(name) or default
        if value not in allowed:
            self._problems.append(f"{name} must be one of {', '.join(allowed)}, got {value!r}")
            return default
        return value

    def finish(self) -> None:
        """Raise :class:`ConfigError` listing every recorded problem, if there are any."""
        if self._problems:
            raise ConfigError("invalid configuration: " + "; ".join(self._problems))
