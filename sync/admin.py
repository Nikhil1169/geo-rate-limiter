"""Partition-simulation admin endpoints for the sync service.

Endpoints
  POST /admin/partition  {"from": "us", "to": "eu"}
      Start silently dropping messages that originated in "us" before they
      are applied to this region.  One-directional: to simulate a full split
      you need two calls (us→eu and eu→us).

  POST /admin/heal  {"from": "us", "to": "eu"}
      Remove the partition.  The next reconciliation tick (≤30s) will bring
      the isolated region back in sync.

  GET  /admin/status
      List all active (from, to) partition pairs on this node.

Implementation note
  PARTITIONS is imported from sync_service at build_app() call time (not
  at module load time) to avoid a circular import.  By the time build_app()
  is invoked from sync_service.run(), sync_service is fully initialised and
  PARTITIONS already exists in sys.modules.
"""

from __future__ import annotations

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field, field_validator

from sync.state import PARTITIONS

_VALID_REGIONS = frozenset({"us", "eu", "asia"})


class PartitionRequest(BaseModel):
    from_region: str = Field(alias="from")
    to_region: str = Field(alias="to")

    model_config = {"populate_by_name": True}

    @field_validator("from_region", "to_region")
    @classmethod
    def _valid_region(cls, v: str) -> str:
        if v not in _VALID_REGIONS:
            raise ValueError(f"{v!r} is not a valid region; choose from {sorted(_VALID_REGIONS)}")
        return v


def build_app() -> FastAPI:
    app = FastAPI(title="sync-admin", version="1.0.0")

    @app.post("/admin/partition")
    async def add_partition(req: PartitionRequest):
        if req.from_region == req.to_region:
            raise HTTPException(status_code=400, detail="from and to must be different regions")
        key = (req.from_region, req.to_region)
        already = key in PARTITIONS
        PARTITIONS.add(key)
        return {
            "status": "already_partitioned" if already else "partitioned",
            "from": req.from_region,
            "to": req.to_region,
            "active_partitions": len(PARTITIONS),
        }

    @app.post("/admin/heal")
    async def heal_partition(req: PartitionRequest):
        key = (req.from_region, req.to_region)
        if key not in PARTITIONS:
            raise HTTPException(
                status_code=404,
                detail=f"No active partition {req.from_region!r}→{req.to_region!r}",
            )
        PARTITIONS.discard(key)
        return {
            "status": "healed",
            "from": req.from_region,
            "to": req.to_region,
            "active_partitions": len(PARTITIONS),
        }

    @app.get("/admin/status")
    async def get_status():
        return {
            "partitions": [
                {"from": f, "to": t}
                for f, t in sorted(PARTITIONS)
            ]
        }

    return app
