"""Configuration read from the environment."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from chassis.settings import EnvReader


@dataclass(frozen=True, slots=True)
class Settings:
    """Everything the gateway needs to start."""

    search_url: str
    port: int
    log_level: str
    trace_sample_rate: float

    @classmethod
    def from_env(cls, environ: Mapping[str, str]) -> Settings:
        """Read and validate the environment; raise ``ConfigError`` listing every problem."""
        reader = EnvReader(environ)
        settings = cls(
            search_url=reader.text("SEARCH_URL"),
            port=reader.integer("PORT", 8080, minimum=1, maximum=65535),
            log_level=reader.choice("LOG_LEVEL", "INFO", ("DEBUG", "INFO", "WARNING", "ERROR")),
            trace_sample_rate=reader.number("TRACE_SAMPLE_RATE", 0.0, minimum=0.0, maximum=1.0),
        )
        reader.finish()
        return settings
