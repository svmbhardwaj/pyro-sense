"""Track-prediction stage tests — pure logic, no live DB (§10/§27 stage).

Covers the three contract-critical pure functions:
- spatial.risk_block_from_rows — the temporal risk block is NEVER fabricated:
  empty → insufficient_history, stale rows ignored, missing horizons stay None.
- track_predictions.track_abstention — unknown label / low confidence abstain
  with a reason, confident calls do not (plan §27).
- track_predictions.fresh_track_ids — staleness-driven selection only
  re-classifies tracks with new observations after their last prediction.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.data.spatial import risk_block_from_rows
from app.db.models import HotspotTrack, RiskPrediction
from app.pipeline.track_predictions import (
    ABSTAIN_CONFIDENCE,
    fresh_track_ids,
    prediction_row_for_track,
    track_abstention,
)

H3_CELL = "871f1b0bffffff"
NOW = datetime(2026, 10, 13, 12, 0, tzinfo=timezone.utc)


def _risk_row(horizon: str, prob: float, *, ts: datetime = NOW, overall: str = "LOW") -> RiskPrediction:
    return RiskPrediction(
        id={"1day": 1, "3day": 2, "7day": 3}[horizon],
        h3_cell=H3_CELL,
        horizon=horizon,
        probability=prob,
        level="HIGH" if prob >= 0.6 else "LOW",
        overall=overall,
        model_version="1.0.0",
        feature_schema_version="2.0.0",
        data_timestamp=ts,
    )


class TestRiskBlockFromRows:
    def test_empty_is_insufficient_history_never_fabricated(self):
        block = risk_block_from_rows([], h3_cell=H3_CELL)
        assert block["status"] == "insufficient_history"
        assert block["risk_1d"] is None and block["risk_3d"] is None and block["risk_7d"] is None
        assert block["overall"] is None

    def test_all_horizons_ok_merges_newest_batch(self):
        rows = [
            _risk_row("1day", 0.82, overall="HIGH"),
            _risk_row("3day", 0.73),
            _risk_row("7day", 0.51),
        ]
        block = risk_block_from_rows(rows, h3_cell=H3_CELL)
        assert block["status"] == "ok"
        assert block["risk_1d"] == 0.82
        assert block["risk_3d"] == 0.73
        assert block["risk_7d"] == 0.51
        assert block["overall"] == "HIGH"
        assert block["data_timestamp"] == NOW.isoformat()

    def test_stale_rows_are_ignored(self):
        stale = NOW - timedelta(days=1)
        rows = [
            _risk_row("1day", 0.99, ts=stale),
            _risk_row("1day", 0.10),  # newest batch only
        ]
        block = risk_block_from_rows(rows, h3_cell=H3_CELL)
        assert block["status"] == "ok"
        assert block["risk_1d"] == 0.10

    def test_missing_horizon_stays_none(self):
        rows = [_risk_row("1day", 0.4), _risk_row("3day", 0.4)]  # 7day row absent
        block = risk_block_from_rows(rows, h3_cell=H3_CELL)
        assert block["status"] == "ok"
        assert block["risk_7d"] is None


class TestTrackAbstention:
    def test_unknown_label_abstains(self):
        assert track_abstention("unknown", 0.99) == (True, "unknown_label")

    def test_low_confidence_abstains_with_reason(self):
        conf = ABSTAIN_CONFIDENCE - 0.01
        assert track_abstention("forest_vegetation", conf) == (True, "low_confidence")

    def test_confident_known_label_does_not_abstain(self):
        assert track_abstention("forest_vegetation", 0.9) == (False, None)


class TestFreshTrackIds:
    @staticmethod
    def _track(tid: int, last_seen: datetime) -> HotspotTrack:
        # Plain ORM construction (no session needed — pure instance attrs).
        return HotspotTrack(
            id=tid,
            track_uid=f"trk-{tid}",
            status="active",
            last_seen=last_seen,
            first_seen=last_seen,
            centroid_lat=20.0,
            centroid_lng=75.0,
        )

    def test_tracks_without_prediction_are_selected(self):
        t = self._track(1, NOW)
        assert fresh_track_ids([t], {}) == [t]

    def test_track_with_new_observations_is_reclassified(self):
        t = self._track(2, NOW)
        pred_at = NOW - timedelta(hours=1)  # prediction older than last_seen
        assert fresh_track_ids([t], {2: pred_at}) == [t]

    def test_freshly_predicted_track_is_skipped(self):
        t = self._track(3, NOW)
        pred_at = NOW + timedelta(hours=1)  # prediction made after last_seen
        assert fresh_track_ids([t], {3: pred_at}) == []

    def test_input_order_preserved_for_capped_batches(self):
        a = self._track(1, NOW)
        b = self._track(2, NOW - timedelta(hours=1))
        out = fresh_track_ids([a, b], {1: NOW + timedelta(hours=1)})  # a fresh → skipped
        assert out == [b]


class TestPredictionRowShape:
    def test_row_carries_classification_risk_and_versions(self):
        track = self._built_track()
        classification = {
            "label": "forest_vegetation",
            "confidence": 0.91,
            "abstained": False,
            "abstain_reason": None,
            "probabilities": {"forest_vegetation": 0.91},
            "model_version": "1.0.0",
            "feature_schema_version": "2.0.0",
            "feature_snapshot": {"frp_mean": 12.5},
        }
        risk_block = {
            "h3_cell": H3_CELL,
            "risk_1d": 0.5,
            "risk_3d": None,
            "risk_7d": None,
            "status": "ok",
            "overall": "MODERATE",
            "model_version": "1.0.0",
            "data_timestamp": NOW.isoformat(),
        }
        row = prediction_row_for_track(track, classification=classification, risk_block=risk_block, feature_snapshot=classification["feature_snapshot"])
        assert row.subject_type == "track"
        assert row.track_id == track.id
        assert row.predicted_label == "forest_vegetation"
        assert row.risk_1d == 0.5 and row.risk_3d is None
        assert row.risk_status == "ok"
        assert row.abstained is False

    @staticmethod
    def _built_track() -> HotspotTrack:
        return HotspotTrack(
            id=7,
            track_uid="trk-7",
            status="active",
            last_seen=NOW,
            first_seen=NOW,
            centroid_lat=20.0,
            centroid_lng=75.0,
        )


class TestAbstainConstant:
    def test_threshold_is_bounded_from_zero(self):
        # A confidence threshold of 0 would leave every borderline call
        # presented as confident — abstention (plan §27) requires the gate.
        assert 0.0 < ABSTAIN_CONFIDENCE < 1.0
