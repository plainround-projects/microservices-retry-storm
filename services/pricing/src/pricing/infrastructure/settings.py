"""Configuration read from the environment."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from chassis.settings import EnvReader


@dataclass(frozen=True, slots=True)
class Settings:
    """Everything pricing needs to start."""

    stock_url: str
    port: int
    log_level: str

    @classmethod
    def from_env(cls, environ: Mapping[str, str]) -> Settings:
        """Read and validate the environment; raise ``ConfigError`` listing every problem."""
        reader = EnvReader(environ)
        settings = cls(
            stock_url=reader.text("STOCK_URL"),
            port=reader.integer("PORT", 8080, minimum=1, maximum=65535),
            log_level=reader.choice("LOG_LEVEL", "INFO", ("DEBUG", "INFO", "WARNING", "ERROR")),
        )
        reader.finish()
        return settings
