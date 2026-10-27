"""Process entry point: ``pricing-serve``."""

from __future__ import annotations

import asyncio
import logging
import os
import sys

import uvicorn

from chassis.errors import ConfigError
from chassis.logging import configure_logging
from pricing.infrastructure.settings import Settings
from pricing.interfaces.bootstrap import build_app


async def _serve(settings: Settings) -> None:
    app = build_app(settings)
    config = uvicorn.Config(
        app, host="0.0.0.0", port=settings.port, log_config=None, timeout_graceful_shutdown=10, backlog=4096
    )
    await uvicorn.Server(config).serve()


def main() -> None:
    """Read the environment, build the application and serve until SIGTERM, then drain in-flight requests."""
    try:
        settings = Settings.from_env(os.environ)
    except ConfigError as error:
        configure_logging("pricing")
        logging.getLogger("pricing").error(str(error))
        sys.exit(2)
    configure_logging("pricing", settings.log_level)
    asyncio.run(_serve(settings))


if __name__ == "__main__":
    main()
