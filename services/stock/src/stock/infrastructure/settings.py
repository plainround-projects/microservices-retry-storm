"""Configuration read from the environment."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from chassis.settings import EnvReader


@dataclass(frozen=True, slots=True)
class Settings:
    """Everything the stock store needs to start."""

    database_url: str
    port: int
    log_level: str
    capacity: int
    latency_ms: int
    fail_rate: float

    @classmethod
    def from_env(cls, environ: Mapping[str, str]) -> Settings:
        """Read and validate the environment; raise ``ConfigError`` listing every problem."""
        reader = EnvReader(environ)
        settings = cls(
            database_url=reader.text("DATABASE_URL"),
            port=reader.integer("PORT", 8080, minimum=1, maximum=65535),
            log_level=reader.choice("LOG_LEVEL", "INFO", ("DEBUG", "INFO", "WARNING", "ERROR")),
            capacity=reader.integer("STOCK_CONCURRENCY", 16, minimum=1, maximum=1000),
            latency_ms=reader.integer("LATENCY_MS", 50, minimum=0, maximum=60_000),
            fail_rate=reader.number("FAIL_RATE", 0.0, minimum=0.0, maximum=1.0),
        )
        reader.finish()
        return settings
