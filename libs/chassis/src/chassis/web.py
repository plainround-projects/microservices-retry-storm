"""Health routes."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Response

from chassis.health import HealthRegistry


def health_router(registry: HealthRegistry) -> APIRouter:
    """Routes ``/health/live`` (always 200) and ``/health/ready`` (503 while a dependency is down)."""
    router = APIRouter(tags=["health"])

    @router.get("/health/live")
    async def live() -> dict[str, str]:
        return {"status": "up"}

    @router.get("/health/ready")
    async def ready(response: Response) -> dict[str, Any]:
        report = registry.run()
        if not report.ready:
            response.status_code = 503
        return {"status": "ready" if report.ready else "not ready", "checks": report.checks}

    return router
