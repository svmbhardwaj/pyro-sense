"""Observation sync — live_detections → hotspot_observations (plan §10, §48).

The canonical observation store (Phase A) was built but written by nothing:
raw FIRMS rows kept landing in live_detections only. This stage bridges the
two: every LiveDetection becomes one canonical HotspotObservation row with
full provenance (sensor, satellite, instrument, source, batch id) and a
geography point, idempotent on the natural key (source, satellite, acq_date,
acq_time, latitude, longitude — uq_hotspot_observations_natural_key).

Idempotency: re-running a day never duplicates an observation; the returned
counter counts only rows THIS pass inserted (natural-key conflicts are
absorbed by ON CONFLICT DO NOTHING, exactly like the live_detections store).

Date-window selection: callers pass a day window (the orchestrator syncs the
same window the ingest cycle covers). Overlap is safe — duplicates are
absorbed by the natural key, so the window is an efficiency bound, not a
correctness requirement.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timezone

from geoalchemy2 import Geography
from sqlalchemy import cast, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import HotspotObservation, LiveDetection

logger = logging.getLogger("pyrosense.ml.pipeline.observe")

# Batch size for INSERT ... VALUES lists. Each row binds ~20 federated params
# (18 columns + the geometry cast's lat/lng/SRID); asyncpg rejects any query
# with more than 32,767 bind parameters, so the batch must stay under
# 32767 / 20 ≈ 1638 rows. 1500 keeps headroom for column additions.
OBSERVE_BATCH = 1500


def _make_geom_expr(lat: float, lng: float):
    """The observation's geography point as a bound SQL expression.

    PostGIS takes (lng, lat) — a swapped literal would silently index the
    wrong hemisphere. Cast to the same geography(POINT,4326) type the column
    declares so the GiST index stays usable.
    """
    return cast(
        func.ST_SetSRID(func.ST_MakePoint(lng, lat), 4326),
        Geography(geometry_type="POINT", srid=4326),
    )


def _sensor_tag(satellite: str | None, instrument: str | None) -> str:
    """Canonical sensor tag from FIRMS provenance (pure).

    VIIRS rows: satellite like 'NOAA-20/NOAA-21/SNPP' (or '(Suomi-NPP)'), any
    instrument. MODIS rows: 'Terra'/'Aqua'. Unknown strings pass through
    truncated — provenance is never merged away (plan §9).
    """
    sat = (satellite or "").upper()
    instr = (instrument or "").upper()
    if "NOAA" in sat or "SUOMI" in sat or "NPP" in sat or instr.startswith("VNP"):
        return "VIIRS"
    if "TERRA" in sat or "AQUA" in sat or instr.startswith("MOD"):
        return "MODIS"
    return (satellite or "unknown")[:16]


def normalize_ts(acq_date: date, acq_time: str) -> datetime:
    """acq_date + acq_time (HHMM, nominally UTC in FIRMS) → aware UTC datetime."""
    t = (acq_time or "0000").zfill(4)
    hh = min(int(t[:2]), 23)
    mm = min(int(t[2:4]), 59)
    return datetime(acq_date.year, acq_date.month, acq_date.day, hh, mm, tzinfo=timezone.utc)


def observation_row_from_live(d: LiveDetection) -> dict:
    """One LiveDetection → one canonical observation insert dict (pure)."""
    # LiveDetection carries only the split FIRMS fields (plan §9) — the
    # canonical timestamp is always derived right here.
    ts = normalize_ts(d.acq_date, d.acq_time)
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return {
        "latitude": float(d.latitude),
        "longitude": float(d.longitude),
        # The geom expression is built by _prepare_batch (needs the cast
        # machinery, not a pure literal) — kept out of the pure dict.
        "acq_date": d.acq_date,
        "acq_time": d.acq_time or "0000",
        "acq_timestamp": ts,
        "sensor": _sensor_tag(d.satellite, d.instrument),
        "satellite": d.satellite or "unknown",
        "instrument": d.instrument,
        "source": d.source or "firms",
        "ingest_batch_id": d.ingest_batch_id,
        "frp": float(d.frp or 0.0),
        "brightness_temp": d.bright_ti4,
        "brightness_temp2": d.bright_ti5,
        "confidence": d.confidence,
        # FIRMS scan/track half-angle is not persisted on LiveDetection —
        # readable via getattr so a future column doesn't need this mapper
        # touched ( HotspotObservation.scan stays nullable ).
        "scan": getattr(d, "scan", None),
        "daynight": d.daynight,
        "region": d.region,
    }


def observation_rows_for_insert(rows: list[dict]) -> list[dict]:
    """Add the geography point expression to each pure dict (single caller)."""
    return [
        {**r, "geom": _make_geom_expr(r["latitude"], r["longitude"])}
        for r in rows
    ]


async def _insert_batch(session: AsyncSession, batch: list[dict]) -> int:
    stmt = (
        pg_insert(HotspotObservation)
        .values(batch)
        .on_conflict_do_nothing(constraint="uq_hotspot_observations_natural_key")
    )
    result = await session.execute(stmt)
    await session.flush()
    # rowcount = rows actually inserted (conflicts are not counted).
    return int(result.rowcount or 0)


async def sync_observations(session: AsyncSession, *, day_from: date, day_to: date) -> dict:
    """Canonicalize all live detections whose acq_date ∈ [day_from, day_to].

    Returns {selected, inserted} — inserted counts only NEW observations.
    """
    stmt = select(LiveDetection).where(
        LiveDetection.acq_date >= day_from,
        LiveDetection.acq_date <= day_to,
    )
    dets = (await session.execute(stmt)).scalars().all()
    if not dets:
        return {"selected": 0, "inserted": 0}

    inserted = 0
    batch: list[dict] = []
    for d in dets:
        batch.append(observation_row_from_live(d))
        if len(batch) >= OBSERVE_BATCH:
            inserted += await _insert_batch(session, observation_rows_for_insert(batch))
            batch = []
    if batch:
        inserted += await _insert_batch(session, observation_rows_for_insert(batch))

    logger.info(
        "observation sync: %s selected, %s inserted (%s..%s)",
        len(dets), inserted, day_from.isoformat(), day_to.isoformat(),
    )
    return {"selected": len(dets), "inserted": inserted}
