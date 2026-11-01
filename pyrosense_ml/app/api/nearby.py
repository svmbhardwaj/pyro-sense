"""GET /api/v2/hotspots/nearby — the P0.1 server-authoritative 50 km search.

The browser NEVER computes the authoritative nearby list (plan P0.1): it
sends a coordinate, the database answers with exact geography distances.
Canonical hotspot_tracks first; the legacy seeded hotspots answer while the
tracking pipeline has not yet populated tracks — the response says which
store served it, so nobody mistakes fallback data for canonical data.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.health import get_db
from app.contracts import CONTRACT_VERSION
from app.data.spatial import find_legacy_hotspots_near, find_tracks_near

router = APIRouter(prefix="/api/v2")

logger = logging.getLogger("pyrosense.ml.api.nearby")


class NearbyMeta(BaseModel):
    source: str = Field(description="'hotspot_tracks' (canonical) or 'legacy_hotspots' (fallback)")
    fallback_used: bool
    results_count: int


class NearbyResponseBody(BaseModel):
    center: dict[str, float]
    radius_km: float
    results: list[dict]
    meta: NearbyMeta
    contract_version: str


@router.get("/hotspots/nearby", response_model=NearbyResponseBody)
async def hotspots_nearby(
    lat: float = Query(alias="lat", ge=-90, le=90),
    lng: float = Query(alias="lng", ge=-180, le=180),
    radius_km: float = Query(default=50.0, gt=0, le=500),
    limit: int = Query(default=50, ge=1, le=500),
    since_days: int = Query(default=7, ge=1, le=365),
    session: AsyncSession = Depends(get_db),
):
    # 1. Canonical tracks (spatial index, exact geography distances).
    try:
        tracks = await find_tracks_near(
            session,
            lat=lat,
            lng=lng,
            radius_km=radius_km,
            since_days=since_days,
            limit=limit,
        )
        if tracks:
            return NearbyResponseBody(
                center={"latitude": lat, "longitude": lng},
                radius_km=radius_km,
                results=[t.to_dict() for t in tracks],
                meta=NearbyMeta(source="hotspot_tracks", fallback_used=False, results_count=len(tracks)),
                contract_version=CONTRACT_VERSION,
            )
    except Exception:  # noqa: BLE001 — degraded search must never 500 (§74 E)
        logger.exception("nearby: canonical track search failed — falling back")

    # 2. Legacy seeded hotspots (same geography query, different table).
    legacy = await find_legacy_hotspots_near(
        session,
        lat=lat,
        lng=lng,
        radius_km=radius_km,
        limit=limit,
    )
    return NearbyResponseBody(
        center={"latitude": lat, "longitude": lng},
        radius_km=radius_km,
        results=[t.to_dict() for t in legacy],
        meta=NearbyMeta(source="legacy_hotspots", fallback_used=True, results_count=len(legacy)),
        contract_version=CONTRACT_VERSION,
    )
