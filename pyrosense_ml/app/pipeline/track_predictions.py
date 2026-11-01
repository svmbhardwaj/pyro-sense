"""Track prediction stage — classify tracks + attach GRU risk (plan §10, §27, §31).

Nothing wrote HotspotPrediction(subject_type="track") rows before this stage
existed, so the nearby endpoint's canonical classification/risk blocks were
structurally always null. This closes that loop INCREMENTALLY: each run picks
a bounded batch of active tracks lacking a fresh prediction (newest last_seen
first), engineers the full 43-feature vector at the track centroid (the exact
same path /predict uses), classifies with the current MLP, and attaches the
latest stored GRU risk of the centroid's H3-r7 cell — written as one canonical
HotspotPrediction row per classification pass.

Design constraints:
- Capped per run (TRACK_PREDICTION_BATCH): a full backfill of ~30k tracks at
  external-API-dependent ~seconds per centroid is a multi-night background
  process, not a request — the pipeline's job-run bookkeeping tolerates
  partial completion and fills in progressively.
- Idempotent/staleness-driven (plan §48 spirit): a track is skipped while its
  latest track-level prediction is newer than the track's last_seen — new
  observations are the only reason to re-classify.
- Abstention is part of the contract (plan §27): an unrecognized label or a
  below-threshold confidence is stored as abstained=True with the reason,
  never dressed up as a confident call. The mapping to ontology v2
  (normalize_to_ontology) happens here so every consumer sees canonical labels.
- Risk is read, not inferred: the GRU models need a 30-day sequence, which is
  risk_batch's job. A cell with no horizons gets risk_status=
  "insufficient_history" with null horizons — never fabricated (plan §5).
"""

from __future__ import annotations

import asyncio
import logging

from datetime import datetime
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import HotspotPrediction, HotspotTrack
from app.data.spatial import latest_risk_for_cell
from app.ml.inference import predict as run_inference
from app.features.engineer import engineer_features
from app.ontology import ONTOLOGY_VERSION, normalize_to_ontology

logger = logging.getLogger("pyrosense.ml.pipeline.track_predictions")

# Tracks classified per stage run (documented default; raised only with
# monitoring — each track costs external API calls for its feature vector).
TRACK_PREDICTION_BATCH = 25

# Confidence below which the canonical row abstains (needs_review) instead of
# presenting a borderline call as confident. Deterministic, capped away from
# 0 so a real signal is kept; product-tunable.
ABSTAIN_CONFIDENCE = 0.35

# Classification → the canonical severity vocabulary (HotspotPrediction.severity):
# the ontology v2 concept THIS store carries per prediction row. The ML
# classifier's class + confidence are stored as-is alongside.
SEVERITY_BY_OVERALL: dict[str, str] = {
    "HIGH": "HIGH",
    "MODERATE": "MODERATE",
    "LOW": "LOW",
}


def track_abstention(label: str, confidence: float) -> tuple[bool, str | None]:
    """Contract abstention rule (pure): unknown label or low confidence."""
    if label == "unknown":
        return True, "unknown_label"
    if confidence < ABSTAIN_CONFIDENCE:
        return True, "low_confidence"
    return False, None


def fresh_track_ids(
    tracks: list[HotspotTrack],
    latest_predicted_at: dict[int, datetime],
) -> list[HotspotTrack]:
    """Pure selector: tracks whose newest track-level prediction is stale.

    A track needs classification when it has NO track-level prediction, or
    when new observations arrived after the last one was made
    (predicted_at <= last_seen). Input order (newest last_seen first) is
    preserved so a capped batch always covers the most current tracks.
    """
    out: list[HotspotTrack] = []
    for track in tracks:
        pred_at = latest_predicted_at.get(track.id)
        if pred_at is None or pred_at <= track.last_seen:
            out.append(track)
    return out


async def select_tracks_for_classification(
    session: AsyncSession,
    *,
    limit: int = TRACK_PREDICTION_BATCH,
) -> list[HotspotTrack]:
    """Bounded candidate set: newest open tracks lacking a fresh prediction."""
    candidates = (
        (
            await session.execute(
                select(HotspotTrack)
                .where(HotspotTrack.status.in_(("active", "dormant")))
                .order_by(HotspotTrack.last_seen.desc())
                .limit(limit * 3)  # oversample; the staleness filter trims
            )
        )
        .scalars()
        .all()
    )

    latest: dict[int, datetime] = {}
    if candidates:
        pred_rows = (
            (
                await session.execute(
                    select(HotspotPrediction)
                    .where(
                        HotspotPrediction.subject_type == "track",
                        HotspotPrediction.track_id.in_([t.id for t in candidates]),
                    )
                    .order_by(HotspotPrediction.track_id, HotspotPrediction.predicted_at.desc())
                )
            )
            .scalars()
            .all()
        )
        for p in pred_rows:
            latest.setdefault(p.track_id, p.predicted_at)  # first = latest

    return fresh_track_ids(list(candidates), latest)[:limit]


def prediction_row_for_track(
    track: HotspotTrack,
    *,
    classification: dict,
    risk_block: dict,
    feature_snapshot: dict,
) -> HotspotPrediction:
    """Pure row-builder so tests can assert the written shape without a DB."""
    return HotspotPrediction(
        subject_type="track",
        track_id=track.id,
        predicted_label=classification["label"],
        ontology_version=ONTOLOGY_VERSION,
        confidence=classification["confidence"],
        abstained=classification["abstained"],
        abstain_reason=classification["abstain_reason"],
        probabilities=classification["probabilities"],
        risk_1d=risk_block.get("risk_1d"),
        risk_3d=risk_block.get("risk_3d"),
        risk_7d=risk_block.get("risk_7d"),
        risk_status=risk_block.get("status", "insufficient_history"),
        model_version=classification["model_version"],
        feature_schema_version=classification["feature_schema_version"],
        feature_snapshot=feature_snapshot,
    )


async def run_track_predictions_stage(
    session: AsyncSession,
    *,
    limit: int = TRACK_PREDICTION_BATCH,
) -> dict:
    """One capped classification pass over stale tracks. Returns stage stats."""
    tracks = await select_tracks_for_classification(session, limit=limit)
    if not tracks:
        return {"selected": 0, "classified": 0, "abstained": 0, "limit": limit}

    # Feature engineering makes external (OSM / weather / dynamic-world) calls;
    # concurrency is bounded so a full track batch can't stampede free tiers.
    # NOTE: the shared AsyncSession must NOT be touched inside this gather —
    # classification below is deliberately session-free; the session-using
    # risk lookups and row adds happen sequentially afterwards.
    sem = asyncio.Semaphore(4)

    async def _classify(track: HotspotTrack) -> tuple[HotspotTrack, dict, list[str]]:
        async with sem:
            engineered = await engineer_features(
                track.centroid_lat, track.centroid_lng
            )
            result = run_inference(engineered.features)
        label = normalize_to_ontology(result.predicted_class)
        abstained, reason = track_abstention(label, result.confidence)
        return (
            track,
            {
                "label": label,
                "confidence": result.confidence,
                "abstained": abstained,
                "abstain_reason": reason,
                "probabilities": result.probabilities,
                "model_version": result.model_version,
                "feature_schema_version": result.feature_schema_version,
                "feature_snapshot": dict(result.features),
                "warnings": engineered.warnings,
            },
            [],
        )

    classified_tracks = await asyncio.gather(*(_classify(t) for t in tracks))

    abstained = 0
    written = 0
    for track, classification, _warn in classified_tracks:
        # Sequential: single shared AsyncSession (one indexed query per track).
        risk_block = await latest_risk_for_cell(
            session, lat=track.centroid_lat, lng=track.centroid_lng
        )
        session.add(
            prediction_row_for_track(
                track,
                classification=classification,
                risk_block=risk_block,
                feature_snapshot=classification["feature_snapshot"],
            )
        )
        if classification["abstained"]:
            abstained += 1
        written += 1

    await session.flush()

    classified = written
    logger.info(
        "track predictions: selected=%d classified=%d abstained=%d",
        len(tracks),
        classified,
        abstained,
    )
    return {
        "selected": len(tracks),
        "classified": classified,
        "abstained": abstained,
        "limit": limit,
    }
