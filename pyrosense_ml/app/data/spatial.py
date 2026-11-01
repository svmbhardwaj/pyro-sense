"""Spatial queries — PostGIS is the authority (plan §12, P0.1).

The nearby search answers: "which persistent hotspot tracks are within
R km of this coordinate?" with an exact geography ST_DWithin (never a
bounding box, never client-side Haversine). Ordering is by true distance.

Two stores, tried in order:
1. hotspot_tracks (canonical Phase A entity) — spatial index on centroid_geom.
2. Legacy `hotspots` (seed + live) — same geography query, used while the
   tracking pipeline has not yet populated canonical tracks, so the endpoint
   is useful on day one (plan §74 Scenario B/E: degrade, don't fail).

Every result carries the canonical contract shape (app/contracts.py):
classification mapped through ontology v2, temporal risk marked
insufficient_history when no GRU horizons exist — a legacy risk_score is
exposed as SEVERITY, never dressed up as 1/3/7-day risk (plan §5).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import h3
from geoalchemy2 import Geography
from sqlalchemy import Select, cast, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.contracts import CONTRACT_VERSION
from app.db.models import (
    Hotspot,
    HotspotPrediction,
    HotspotTrack,
    Prediction,
    RiskPrediction,
)
from app.feature_schema import RISK_MODEL_VERSION
from app.ontology import ONTOLOGY_VERSION, normalize_to_ontology

logger = logging.getLogger("pyrosense.ml.spatial")


@dataclass
class NearbyResult:
    """One nearby hit, contract-shaped (plain dict via to_dict())."""

    hotspot_id: str
    latitude: float
    longitude: float
    distance_km: float
    status: str
    first_seen: datetime | None = None
    last_seen: datetime | None = None
    observation_count: int = 0
    classification: dict | None = None
    risk: dict | None = None
    severity: dict | None = None
    facility: dict | None = None
    evidence: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "hotspot_id": self.hotspot_id,
            "latitude": self.latitude,
            "longitude": self.longitude,
            "distance_km": round(self.distance_km, 3),
            "status": self.status,
            "first_seen": self.first_seen.isoformat() if self.first_seen else None,
            "last_seen": self.last_seen.isoformat() if self.last_seen else None,
            "observation_count": self.observation_count,
            "classification": self.classification,
            "risk": self.risk,
            "severity": self.severity,
            "facility": self.facility,
            "evidence": self.evidence,
            "contract_version": CONTRACT_VERSION,
        }


def _point_geography(lat: float, lng: float):
    """WGS84 point as geography — the canonical distance space (plan §12)."""
    return cast(
        func.ST_SetSRID(func.ST_MakePoint(lng, lat), 4326),
        Geography(geometry_type="POINT", srid=4326),
    )


def _nearby_tracks_stmt(lat: float, lng: float, radius_m: float, since: datetime, limit: int) -> Select:
    point_geo = _point_geography(lat, lng)
    dist_m = func.ST_Distance(HotspotTrack.centroid_geom, point_geo).label("dist_m")
    return (
        select(HotspotTrack, dist_m)
        .where(
            HotspotTrack.centroid_geom.isnot(None),
            HotspotTrack.status != "closed",
            HotspotTrack.last_seen >= since,
            func.ST_DWithin(HotspotTrack.centroid_geom, point_geo, radius_m),
        )
        .order_by(dist_m)
        .limit(limit)
    )


def _nearby_legacy_stmt(lat: float, lng: float, radius_m: float, limit: int) -> Select:
    point_geo = _point_geography(lat, lng)
    dist_m = func.ST_Distance(Hotspot.geom, point_geo).label("dist_m")
    return (
        select(Hotspot, dist_m)
        .where(
            Hotspot.geom.isnot(None),
            func.ST_DWithin(Hotspot.geom, point_geo, radius_m),
        )
        .order_by(dist_m)
        .limit(limit)
    )


async def find_tracks_near(
    session: AsyncSession,
    *,
    lat: float,
    lng: float,
    radius_km: float,
    since_days: int,
    limit: int,
) -> list[NearbyResult]:
    """Canonical nearby search over hotspot_tracks (PostGIS ST_DWithin)."""
    radius_m = radius_km * 1000.0
    since = datetime.now(timezone.utc) - timedelta(days=since_days)

    rows = (await session.execute(_nearby_tracks_stmt(lat, lng, radius_m, since, limit))).all()
    if not rows:
        return []

    tracks = [r[0] for r in rows]
    dists = [float(r[1]) for r in rows]

    # Latest classification/risk prediction per track (one query for all ids).
    preds: dict[int, HotspotPrediction] = {}
    pred_rows = (
        await session.execute(
            select(HotspotPrediction)
            .where(
                HotspotPrediction.subject_type == "track",
                HotspotPrediction.track_id.in_([t.id for t in tracks]),
            )
            .order_by(HotspotPrediction.track_id, HotspotPrediction.predicted_at.desc())
        )
    ).scalars().all()
    for p in pred_rows:
        preds.setdefault(p.track_id, p)  # first = latest (ordered desc)

    results: list[NearbyResult] = []
    for track, dist_m in zip(tracks, dists):
        p = preds.get(track.id)
        classification = None
        risk = None
        if p is not None:
            label = normalize_to_ontology(p.predicted_label)
            classification = {
                "label": label,
                "confidence": p.confidence,
                "uncertainty": p.uncertainty,
                "abstained": p.abstained,
                "abstain_reason": p.abstain_reason,
                "model_version": p.model_version,
                "ontology_version": p.ontology_version,
            }
            risk = {
                "risk_1d": p.risk_1d,
                "risk_3d": p.risk_3d,
                "risk_7d": p.risk_7d,
                "status": p.risk_status if p.risk_1d is not None else "insufficient_history",
                "model_version": p.model_version,
            }
        facility = None
        if track.facility_id:
            facility = {
                "facility_id": track.facility_id,
                "name": None,
                "match_type": track.facility_match_type or "nearest_facility",
                "distance_km": track.facility_distance_km,
            }
        results.append(
            NearbyResult(
                hotspot_id=track.track_uid,
                latitude=track.centroid_lat,
                longitude=track.centroid_lng,
                distance_km=dist_m / 1000.0,
                status=track.status,
                first_seen=track.first_seen,
                last_seen=track.last_seen,
                observation_count=track.observation_count,
                classification=classification,
                risk=risk,
                facility=facility,
            )
        )
    return results


async def find_legacy_hotspots_near(
    session: AsyncSession,
    *,
    lat: float,
    lng: float,
    radius_km: float,
    limit: int,
) -> list[NearbyResult]:
    """Fallback nearby search over the legacy seeded `hotspots` table.

    Same exact geography query. Legacy rows carry one risk_score — that is a
    severity signal, NOT a temporal probability: reported as severity with
    risk.status="insufficient_history" (plan §5's demanded separation).
    """
    radius_m = radius_km * 1000.0
    rows = (await session.execute(_nearby_legacy_stmt(lat, lng, radius_m, limit))).all()
    if not rows:
        return []

    hotspots = [r[0] for r in rows]
    dists = [float(r[1]) for r in rows]

    # Latest prediction per hotspot (correlated-max join, one query).
    latest = (
        select(
            Prediction.hotspot_id,
            func.max(Prediction.prediction_timestamp).label("max_ts"),
        )
        .group_by(Prediction.hotspot_id)
        .subquery()
    )
    preds: dict[int, Prediction] = {}
    pred_rows = (
        await session.execute(
            select(Prediction)
            .join(
                latest,
                (latest.c.hotspot_id == Prediction.hotspot_id)
                & (latest.c.max_ts == Prediction.prediction_timestamp),
            )
            .where(Prediction.hotspot_id.in_([h.id for h in hotspots]))
        )
    ).scalars().all()
    for p in pred_rows:
        preds[p.hotspot_id] = p

    results: list[NearbyResult] = []
    for h, dist_m in zip(hotspots, dists):
        p = preds.get(h.id)
        classification = None
        severity = None
        if p is not None:
            label = normalize_to_ontology(p.predicted_class)
            classification = {
                "label": label,
                "confidence": p.confidence,
                "uncertainty": None,
                "abstained": label == "unknown",
                "abstain_reason": "legacy_label" if label == "unknown" else None,
                "model_version": p.model_version,
                "ontology_version": ONTOLOGY_VERSION,
            }
            severity = {
                "score": p.risk_score,
                "level": "HIGH" if p.risk_score >= 0.66 else ("MODERATE" if p.risk_score >= 0.33 else "LOW"),
            }
        results.append(
            NearbyResult(
                hotspot_id=h.hotspot_uid,
                latitude=h.latitude,
                longitude=h.longitude,
                distance_km=dist_m / 1000.0,
                status="active",
                first_seen=h.created_at,
                last_seen=h.updated_at,
                observation_count=0,
                classification=classification,
                risk={"risk_1d": None, "risk_3d": None, "risk_7d": None, "status": "insufficient_history"},
                severity=severity,
            )
        )
    return results


# ── GRU temporal risk lookup (plan §5) ─────────────────────────────────────


def risk_block_from_rows(
    rows: list[RiskPrediction],
    *,
    h3_cell: str,
) -> dict:
    """Pure mapper: RiskPrediction rows → the canonical risk block.

    The three horizons live in separate rows per (cell, horizon) unique
    constraint; only the rows sharing the newest data_timestamp are merged.
    A missing horizon stays None with status "ok" only when none of the
    horizons are present; otherwise status reflects what exists (plan §5:
    never fabricate, never dress a scalar up as temporal risk).
    """
    if not rows:
        return {
            "h3_cell": h3_cell,
            "risk_1d": None,
            "risk_3d": None,
            "risk_7d": None,
            "status": "insufficient_history",
            "overall": None,
            "model_version": RISK_MODEL_VERSION,
            "data_timestamp": None,
        }

    latest_ts = max(r.data_timestamp for r in rows)
    current = [r for r in rows if r.data_timestamp == latest_ts]
    by_horizon = {r.horizon: r for r in current}
    present = [h for h in ("1day", "3day", "7day") if h in by_horizon]
    return {
        "h3_cell": h3_cell,
        "risk_1d": by_horizon.get("1day").probability if "1day" in by_horizon else None,
        "risk_3d": by_horizon.get("3day").probability if "3day" in by_horizon else None,
        "risk_7d": by_horizon.get("7day").probability if "7day" in by_horizon else None,
        "status": "ok" if present else "insufficient_history",
        "overall": by_horizon["1day"].overall if "1day" in by_horizon else (current[0].overall if current else None),
        "model_version": current[0].model_version,
        "data_timestamp": current[0].data_timestamp.isoformat() if current else None,
    }


async def latest_risk_for_cell(
    session: AsyncSession,
    *,
    lat: float,
    lng: float,
) -> dict:
    """GRU temporal risk for the point's H3-r7 cell, or insufficient_history.

    One indexed query (ix_risk_predictions_cell) — cheap enough to call on
    every /predict without caching games.
    """
    cell = h3.latlng_to_cell(float(lat), float(lng), 7)
    rows = (
        (await session.execute(select(RiskPrediction).where(RiskPrediction.h3_cell == cell)))
        .scalars()
        .all()
    )
    return risk_block_from_rows(rows, h3_cell=cell)
