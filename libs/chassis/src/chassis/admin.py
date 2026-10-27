"""Admin routes for the experiments: switch the resilience profile, read traces. Bound to localhost by compose."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from chassis.tracing import SpanStore
from resilience.profile import Profile, ProfileHolder


class ProfileBody(BaseModel):
    """``POST /admin/profile``."""

    name: str


def admin_router(holder: ProfileHolder | None, store: SpanStore) -> APIRouter:
    """``/admin/profile`` (when the service has a profile) and ``/admin/traces``.

    These routes change behavior and expose internals. They exist so the experiments can drive the chain;
    the compose file publishes every port on 127.0.0.1 only, and a real deployment would not mount this router.
    """
    router = APIRouter(prefix="/admin", tags=["admin"])

    if holder is not None:

        @router.get("/profile")
        async def read_profile() -> dict[str, str]:
            return {"name": holder.get().value}

        @router.post("/profile")
        async def set_profile(body: ProfileBody) -> dict[str, str]:
            try:
                holder.set(Profile(body.name))
            except ValueError as error:
                raise HTTPException(status_code=422, detail=f"unknown profile {body.name!r}") from error
            return {"name": holder.get().value}

    @router.get("/traces")
    async def list_traces() -> list[str]:
        return store.trace_ids()

    @router.get("/traces/{trace_id}")
    async def read_trace(trace_id: str) -> list[dict[str, Any]]:
        return [span.as_dict() for span in store.trace(trace_id)]

    return router
