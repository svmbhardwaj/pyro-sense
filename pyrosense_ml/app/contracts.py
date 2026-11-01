"""Shared, versioned API contracts (plan §63).

These Pydantic models ARE the canonical wire contract for Phase A+. Node BFF
and the frontend consume this shape; the OpenAPI schema generated from this
service becomes the generator input for the other layers.

Contract versioning: CONTRACT_VERSION bumps on any breaking shape change.
Non-breaking additions do not bump it.

Coordinate order is [lng, lat] everywhere — GeoJSON order — with explicit
field names (latitude/longitude) alongside so no consumer ever guesses.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.ontology import ONTOLOGY_LABELS, OntologyLabel

CONTRACT_VERSION: Final[str] = "1.0.0"

Geometry = Literal["Point"]

# Entity types on the map/UI (plan §83): separate dimension from risk level.
HotspotStatus = Literal["active", "dormant", "closed"]
RiskStatus = Literal["ok", "insufficient_history", "unavailable"]
FacilityMatchType = Literal[
    "known_facility",
    "contained_in_facility",
    "nearest_facility",
    "candidate_industrial_site",
    "unmatched",
]


class Classification(BaseModel):
    model_config = ConfigDict(frozen=True)

    label: OntologyLabel
    confidence: float = Field(ge=0.0, le=1.0)
    uncertainty: float | None = Field(default=None, ge=0.0, le=1.0)
    abstained: bool = False
    abstain_reason: str | None = None
    model_version: str
    ontology_version: str


class Risk(BaseModel):
    """Temporal risk. Null horizons + status != "ok" = no fabricated numbers."""

    model_config = ConfigDict(frozen=True)

    risk_1d: float | None = Field(default=None, ge=0.0, le=1.0)
    risk_3d: float | None = Field(default=None, ge=0.0, le=1.0)
    risk_7d: float | None = Field(default=None, ge=0.0, le=1.0)
    status: RiskStatus = "ok"
    model_version: str | None = None


class Severity(BaseModel):
    model_config = ConfigDict(frozen=True)

    level: Literal["HIGH", "MODERATE", "LOW"] | None = None
    score: float | None = Field(default=None, ge=0.0, le=1.0)


class EvidenceItem(BaseModel):
    model_config = ConfigDict(frozen=True)

    kind: str  # thermal | spatial | temporal | environmental | facility | satellite
    statement: str
    weight: float | None = None
    source: str | None = None
    quality: Literal["high", "partial", "fallback", "missing"] | None = None


class FacilityRef(BaseModel):
    model_config = ConfigDict(frozen=True)

    facility_id: str
    name: str | None = None
    match_type: FacilityMatchType
    distance_km: float | None = Field(default=None, ge=0.0)


class HotspotObservationItem(BaseModel):
    """One raw FIRMS observation as served to consumers."""

    model_config = ConfigDict(frozen=True)

    latitude: float
    longitude: float
    acq_date: date
    acq_time: str
    sensor: str
    satellite: str
    frp: float | None = None
    brightness_temp: float | None = None
    confidence: str | None = None
    daynight: str | None = None
    distance_km: float | None = None


class Hotspot(BaseModel):
    """THE universal hotspot object consumed by the UI (plan §64)."""

    model_config = ConfigDict(frozen=True)

    id: str
    status: HotspotStatus
    latitude: float
    longitude: float
    first_seen: datetime
    last_seen: datetime
    observation_count: int
    distance_km: float | None = None

    classification: Classification | None = None
    risk: Risk | None = None
    severity: Severity | None = None
    facility: FacilityRef | None = None
    observations: list[HotspotObservationItem] = Field(default_factory=list)
    evidence: list[EvidenceItem] = Field(default_factory=list)

    contract_version: str = "1.0.0"


class NearbyRequest(BaseModel):
    """Query params for GET /api/v2/hotspots/nearby (plan P0.1)."""

    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    radius_km: float = Field(default=50.0, gt=0, le=500)
    limit: int = Field(default=50, ge=1, le=500)
    since_days: int = Field(default=7, ge=1, le=365)
    include: list[Literal["classification", "risk", "facility", "observations"]] = Field(
        default_factory=lambda: ["classification", "risk", "facility"]
    )


class NearbyResponse(BaseModel):
    center: dict[str, float]  # {"latitude": ..., "longitude": ...}
    radius_km: float
    results: list[Hotspot]


__all__ = [
    "CONTRACT_VERSION",
    "Classification",
    "EvidenceItem",
    "FacilityMatchType",
    "FacilityRef",
    "Hotspot",
    "HotspotObservationItem",
    "NearbyRequest",
    "NearbyResponse",
    "ONTOLOGY_LABELS",
    "OntologyLabel",
    "Risk",
    "RiskStatus",
    "Severity",
]
