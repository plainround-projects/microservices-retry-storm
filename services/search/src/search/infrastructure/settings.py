"""Configuration read from the environment."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from chassis.settings import EnvReader


@dataclass(frozen=True, slots=True)
class Settings:
    """Everything search needs to start."""

    pricing_url: str
    port: int
    log_level: str
    concurrency_limit: int

    @classmethod
    def from_env(cls, environ: Mapping[str, str]) -> Settings:
        """Read and validate the environment; raise ``ConfigError`` listing every problem."""
        reader = EnvReader(environ)
        settings = cls(
            pricing_url=reader.text("PRICING_URL"),
            port=reader.integer("PORT", 8080, minimum=1, maximum=65535),
            log_level=reader.choice("LOG_LEVEL", "INFO", ("DEBUG", "INFO", "WARNING", "ERROR")),
            concurrency_limit=reader.integer("SEARCH_CONCURRENCY_LIMIT", 40, minimum=1, maximum=10_000),
        )
        reader.finish()
        return settings
