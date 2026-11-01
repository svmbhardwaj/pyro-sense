"""Tracking pipeline — observation → track temporal linkage (plan §15, §40).

A track PERSISTS across small centroid movements: identity is temporal
linkage, not a rounded coordinate. An unlinked observation joins the nearest
OPEN track when BOTH:

1. Spatial: it falls within TRACK_LINK_RADIUS_KM of the track's centroid —
   the same 5 km corroboration radius the Node pipeline uses
   (FACILITY_RADIUS_KM in config/regions.ts): a fire at a fixed industrial
   site drifts tens of meters between passes, not kilometers.
2. Temporal: it is no more than TRACK_LINK_MAX_GAP_DAYS older than the
   track's last_seen — a long absence closes the chapter rather than
   stitching unrelated events together.

Anything else is onset → a NEW track owned by that observation. Deliberately
simple and fully deterministic: same rows in, same tracks out.

Idempotency & derived-stat recomputation (plan §15):
- Only observations with track_id IS NULL are linked; replays skip already
  linked rows, so re-running a window never double-links or refires onset.
- Per-track derived stats (first/last seen, observation_count, unique fire
  days, distinct satellites = sensor_agreement, centroid, radius_km) are
  RECOMPUTED from the linked observations with one GROUP BY, not patched
  incrementally — a corrupted state heals on the next run.
- Status is recomputed (never archived aside): active (seen within
  DORMANT_AFTER_DAYS) → dormant (within CLOSE_AFTER_DAYS) → closed.
  Older tracks that already sit at their target status just stay there;
  a closed track never reopens.

Facility resolution (plan §40): the track's facility match resolves against
the registry Node pushes via /internal/facility-risk/sync (facility_risk_cache
— the only facility set the ML service knows). PostGIS ST_Distance over
geography: within FACILITY_RADIUS_KM → nearest_facility, else unmatched.
Containment match types (known_facility, contained_in_facility) need facility
boundary polygons, which are not synced today — they stay unused until
boundaries ship, by design rather than omission.
"""

from __future__ import annotations

import hashlib
import logging
import math
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone

import h3
from geoalchemy2 import Geography
from sqlalchemy import cast, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import FacilityRiskCache, HotspotObservation, HotspotTrack

logger = logging.getLogger("pyrosense.ml.pipeline.track")

# §15: linkage radius — matches Node's FACILITY_RADIUS_KM corroboration.
TRACK_LINK_RADIUS_KM = 5.0
# §15: max gap between an observation and a track's last_seen before the
# observation opens a new track instead of joining.
TRACK_LINK_MAX_GAP_DAYS = 2
# Status windows (§15).
DORMANT_AFTER_DAYS = 3
CLOSE_AFTER_DAYS = 14
# §40: same corroboration radius as Node's FACILITY_RADIUS_KM.
FACILITY_RADIUS_KM = 5.0

# Perf guards for full-scale runs (one 2-day national fetch is ~130k rows):
# a 10 km grid bucket turns the O(N×M) haversine scan into a 3×3-neighborhood
# lookup (5 km radius < 5 km half-bucket at any latitude, km-space grid).
TRACK_GRID_KM = 10.0
# Sequence ids are preallocated in chunks — a per-onset flush costs 130k
# DB round-trips.
TRACK_ID_CHUNK = 10_000
# asyncpg caps bind parameters at 32,767/statement; keep IN-lists below it.
_QUERY_CHUNK = 20_000
_KM_PER_DEG = 111.195


def _chunks(seq: list, size: int = _QUERY_CHUNK):
    """Bounded slices so every IN-list stays under the asyncpg param cap."""
    for i in range(0, len(seq), size):
        yield seq[i : i + size]

EARTH_RADIUS_KM = 6371.0088


def _point_geo(lat: float, lng: float):
    return cast(
        func.ST_SetSRID(func.ST_MakePoint(lng, lat), 4326),
        Geography(geometry_type="POINT", srid=4326),
    )


# ── Pure linkage logic (unit-testable without a DB) ──────────────────────


def _ts(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    la1, la2 = math.radians(lat1), math.radians(lat2)
    dla = la2 - la1
    dlo = math.radians(lng2 - lng1)
    a = math.sin(dla / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin(dlo / 2) ** 2
    return 2.0 * EARTH_RADIUS_KM * math.asin(min(1.0, math.sqrt(a)))


class TrackLinkCandidate:
    """Structural duck-type for linkage candidates (avoids ORM dependency)."""

    id: int
    centroid_lat: float
    centroid_lng: float
    last_seen: datetime


def try_link(
    obs: dict,
    candidates: list[TrackLinkCandidate],
    *,
    radius_km: float = TRACK_LINK_RADIUS_KM,
    max_gap_days: int = TRACK_LINK_MAX_GAP_DAYS,
    now: datetime | None = None,
) -> int | None:
    """Which candidate track does a new observation join? None = onset.

    obs: {latitude, longitude, acq_timestamp (aware)}. Deterministic: the
    nearest qualifying centroid wins; exact ties break on the lowest track id
    so replays produce identical linkage.
    """
    obs_ts = _ts(obs["acq_timestamp"])
    best: tuple[float, int] | None = None
    for t in candidates:
        # Temporal gate: the observation must not be much older than the
        # track's most recent activity (a gap means a new chapter).
        gap = obs_ts - _ts(t.last_seen)
        if gap > timedelta(days=max_gap_days) or gap < -timedelta(days=max_gap_days):
            continue
        if not (-90.0 <= float(obs["latitude"]) <= 90.0 and -180.0 <= float(obs["longitude"]) <= 180.0):
            continue
        dist = haversine_km(
            float(obs["latitude"]), float(obs["longitude"]), t.centroid_lat, t.centroid_lng
        )
        if dist <= radius_km and (best is None or dist < best[0] or (dist == best[0] and t.id < best[1])):
            best = (dist, t.id)
    return best[1] if best else None


def status_for_last_seen(
    last_seen: datetime,
    *,
    now: datetime | None = None,
    dormant_after_days: int = DORMANT_AFTER_DAYS,
    close_after_days: int = CLOSE_AFTER_DAYS,
) -> str:
    """Status derived from staleness (pure). active → dormant → closed."""
    ref = _ts(now or datetime.now(timezone.utc))
    age = ref - _ts(last_seen)
    if age <= timedelta(days=dormant_after_days):
        return "active"
    if age <= timedelta(days=close_after_days):
        return "dormant"
    return "closed"


def track_uid_for(obs) -> str:
    """Deterministic onset uid derived from the observation identity.

    Replay-safe: re-syncing the same raw row yields the same uid string,
    though linkage normally prevents a second onset anyway.
    """
    raw = (
        f"{obs.source}|{obs.satellite}|{obs.acq_date.isoformat()}|{obs.acq_time}"
        f"|{float(obs.latitude):.6f}|{float(obs.longitude):.6f}"
    )
    return f"HT-{hashlib.sha1(raw.encode()).hexdigest()[:12]}"


def h3_cell_for(lat: float, lng: float, res: int = 8) -> str:
    return h3.latlng_to_cell(float(lat), float(lng), res)


# ── Pipeline stage ──────────────────────────────────────────────────────


async def run_tracking(session: AsyncSession, *, now: datetime | None = None) -> dict:
    """Link every unlinked observation, recompute stats, sweep status."""
    ref = _ts(now or datetime.now(timezone.utc))

    unlinked = (
        (
            await session.execute(
                select(HotspotObservation)
                .where(HotspotObservation.track_id.is_(None))
                .order_by(HotspotObservation.acq_timestamp, HotspotObservation.id)
            )
        )
        .scalars()
        .all()
    )

    tracks = (
        (
            await session.execute(
                select(HotspotTrack).where(HotspotTrack.status != "closed")
            )
        )
        .scalars()
        .all()
    )
    tracks_by_id = {t.id: t for t in tracks}

    # Spatial prefilter: links against only the 3×3 grid neighborhood around
    # each observation — same result set as scanning every open track, since
    # any track within TRACK_LINK_RADIUS_KM must be within one bucket.
    buckets: dict[tuple[float, float], list] = {}

    def _bucket_key(lat: float, lng: float) -> tuple[float, float]:
        # km-space floor so one grid works at every latitude.
        lat_km = float(lat) * _KM_PER_DEG
        lng_km = float(lng) * _KM_PER_DEG * math.cos(math.radians(float(lat)))
        return (math.floor(lat_km / TRACK_GRID_KM), math.floor(lng_km / TRACK_GRID_KM))

    def _candidates(key: tuple[float, float]) -> list:
        out: list = []
        for di in (-1, 0, 1):
            for dj in (-1, 0, 1):
                out.extend(buckets.get((key[0] + di, key[1] + dj), ()))
        return out

    for t in tracks:
        buckets.setdefault(_bucket_key(t.centroid_lat, t.centroid_lng), []).append(t)

    # Onset track PKs are preallocated from the sequence in chunks; setting
    # id directly means the single end-of-pass flush writes every insert.
    _id_pool: list[int] = []

    async def _next_track_id() -> int:
        nonlocal _id_pool
        if not _id_pool:
            rows = await session.execute(
                text(
                    "SELECT nextval(pg_get_serial_sequence('hotspot_tracks', 'id')) "
                    "FROM generate_series(1, :n)"
                ),
                {"n": TRACK_ID_CHUNK},
            )
            _id_pool = [int(r[0]) for r in rows.all()]
            _id_pool.reverse()
        return _id_pool.pop()

    linked_count = 0
    onset_count = 0

    for obs in unlinked:
        track_id = try_link(
            {
                "latitude": float(obs.latitude),
                "longitude": float(obs.longitude),
                "acq_timestamp": obs.acq_timestamp,
            },
            _candidates(_bucket_key(obs.latitude, obs.longitude)),
        )
        if track_id is None:
            track = HotspotTrack(
                id=await _next_track_id(),
                track_uid=track_uid_for(obs),
                status="active",
                centroid_lat=float(obs.latitude),
                centroid_lng=float(obs.longitude),
                centroid_geom=_point_geo(float(obs.latitude), float(obs.longitude)),
                radius_km=0.0,
                first_seen=obs.acq_timestamp,
                last_seen=obs.acq_timestamp,
                observation_count=1,
                unique_fire_days=0,
                cells=[h3_cell_for(float(obs.latitude), float(obs.longitude))],
                persistence_score=0.0,
                sensor_agreement=1,
            )
            session.add(track)
            buckets.setdefault(
                _bucket_key(track.centroid_lat, track.centroid_lng), []
            ).append(track)
            tracks_by_id[track.id] = track
            obs.track_id = track.id
            onset_count += 1
        else:
            obs.track_id = track_id
            linked_count += 1
    if unlinked:
        await session.flush()

    touched_ids = sorted({obs.track_id for obs in unlinked if obs.track_id is not None})

    # 2. One GROUP BY recomputes every derived stat for touched tracks
    #    (IN-lists are chunked under the asyncpg param cap).
    if touched_ids:
        stats_rows: list = []
        pos_rows: list = []
        for chunk_ids in _chunks(touched_ids):
            stats_rows.extend(
                (
                    await session.execute(
                        select(
                            HotspotObservation.track_id.label("tid"),
                            func.count(HotspotObservation.id).label("n"),
                            func.min(HotspotObservation.acq_timestamp).label("first"),
                            func.max(HotspotObservation.acq_timestamp).label("last"),
                            func.avg(HotspotObservation.latitude).label("c_lat"),
                            func.avg(HotspotObservation.longitude).label("c_lng"),
                            func.count(func.distinct(HotspotObservation.acq_date)).label("fire_days"),
                            func.count(func.distinct(HotspotObservation.satellite)).label("sensors"),
                        )
                        .where(HotspotObservation.track_id.in_(chunk_ids))
                        .group_by(HotspotObservation.track_id)
                    )
                ).all()
            )
            pos_rows.extend(
                (
                    await session.execute(
                        select(
                            HotspotObservation.track_id,
                            HotspotObservation.latitude,
                            HotspotObservation.longitude,
                        ).where(HotspotObservation.track_id.in_(chunk_ids))
                    )
                ).all()
            )
        positions: dict[int, list[tuple[float, float]]] = defaultdict(list)
        for tid, lat, lng in pos_rows:
            positions[tid].append((float(lat), float(lng)))

        for tid, n, first, last, c_lat, c_lng, fire_days, sensors in stats_rows:
            t = tracks_by_id.get(tid)
            if t is None:
                continue
            t.first_seen = _ts(first)
            t.last_seen = _ts(last)
            t.centroid_lat = float(c_lat)
            t.centroid_lng = float(c_lng)
            t.centroid_geom = _point_geo(float(c_lat), float(c_lng))
            t.observation_count = int(n)
            t.unique_fire_days = int(fire_days)
            t.sensor_agreement = max(1, int(sensors))
            t.radius_km = _max_member_distance_km(
                (t.centroid_lat, t.centroid_lng), positions.get(tid, [])
            )
            t.status = status_for_last_seen(t.last_seen, now=ref)
        await session.flush()

    # 3. Status sweep: untouched non-closed tracks can still age out.
    stale_rows = (
        (
            await session.execute(
                select(HotspotTrack).where(
                    HotspotTrack.status != "closed",
                    HotspotTrack.last_seen < ref - timedelta(days=CLOSE_AFTER_DAYS),
                )
            )
        )
        .scalars()
        .all()
    )
    for t in stale_rows:
        t.status = "closed"

    summary = {
        "unlinked_seen": len(unlinked),
        "linked": linked_count,
        "onset": onset_count,
        "stats_recomputed": len(touched_ids),
        "closed": len(stale_rows),
        "open_tracks": len(tracks_by_id),
    }
    logger.info("tracking done: %s", summary)
    return summary


def _max_member_distance_km(
    centroid: tuple[float, float], members: list[tuple[float, float]]
) -> float:
    """Max centroid→member distance in km (pure; the phenomenon's wander)."""
    if not members:
        return 0.0
    return round(max(haversine_km(centroid[0], centroid[1], m[0], m[1]) for m in members), 3)


# ── §40 facility resolution ─────────────────────────────────────────────


async def resolve_track_facilities(
    session: AsyncSession,
    *,
    radius_km: float = FACILITY_RADIUS_KM,
    limit_per_track: int = 1,
) -> dict:
    """Resolve facility_id / facility_match_type / facility_distance_km for
    every non-closed track against Node's pushed facility registry.

    Registry = FacilityRiskCache rows (ids Node synced). One batched SQL pass
    computes ST_Distance(geography) between every (track, facility) pair; the
    nearest facility within radius wins. Beyond radius → unmatched. All
    non-closed tracks are re-resolved every run (nightly track counts are
    tens; correctness beats cleverness).

    Contains a pure-logic twin (resolve_facilities_pure) for tests.
    """
    # 20k-chunked pagination of the exposed registry would over-scan; nightly
    # track counts are modest (< tens of thousands), so one full fetch here
    # stays the simple correct shape.
    tracks = (
        (
            await session.execute(
                select(HotspotTrack).where(HotspotTrack.status != "closed")
            )
        )
        .scalars()
        .all()
    )
    if not tracks:
        return {"tracks": 0, "matched": 0, "unmatched": 0}

    # facilities registry: no limit — the pushes Node makes are already
    # bounded (≤ 20k rows) by the /internal/facility-risk/sync contract.
    facilities = (
        (await session.execute(select(FacilityRiskCache))).scalars().all()
    )
    tracks_flat = [
        (t.id, float(t.centroid_lat), float(t.centroid_lng), t) for t in tracks
    ]
    facs_flat = [
        (f.facility_id, float(f.latitude), float(f.longitude)) for f in facilities
    ]

    resolved = resolve_facilities_pure(
        tracks=[(tid, lat, lng) for tid, lat, lng, _ in tracks_flat],
        facilities=facs_flat,
        radius_km=radius_km,
    )

    matched = 0
    unmatched = 0
    track_by_id = {tid: t for tid, _lat, _lng, t in tracks_flat}  # O(1) lookup
    for tid, facility in resolved.items():
        t = track_by_id[tid]
        if facility is None:
            t.facility_id = None
            t.facility_match_type = "unmatched"
            t.facility_distance_km = None
            unmatched += 1
        else:
            t.facility_id = facility["facility_id"]
            t.facility_match_type = "nearest_facility"
            t.facility_distance_km = facility["distance_km"]
            matched += 1
    await session.flush()

    summary = {"tracks": len(tracks), "matched": matched, "unmatched": unmatched}
    logger.info("facility resolution: %s", summary)
    return summary


def resolve_facilities_pure(
    *,
    tracks: list[tuple[int, float, float]],
    facilities: list[tuple[str, float, float]],
    radius_km: float = FACILITY_RADIUS_KM,
) -> dict[int, dict | None]:
    """Pure facility resolution (no DB): track (id, lat, lng) × facility
    (id, lat, lng) → nearest within radius, as {track_id: {facility_id,
    distance_km} | None}. Mirrors what the PostGIS ST_Distance query
    returns; used by the async stage and directly testable.
    """
    out: dict[int, dict | None] = {}
    for tid, tlat, tlng in tracks:
        best: tuple[float, str] | None = None
        for fid, flat, flng in facilities:
            d = haversine_km(tlat, tlng, flat, flng)
            if d <= radius_km and (best is None or d < best[0]):
                best = (d, fid)
        out[tid] = (
            {"facility_id": best[1], "distance_km": round(best[0], 3)}
            if best
            else None
        )
    return out
