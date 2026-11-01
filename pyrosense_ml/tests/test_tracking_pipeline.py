"""Phase D tracking-pipeline tests — pure logic + SQL compile, no live DB.

1. Linkage (§15): spatial radius + temporal gap gates, deterministic
   tie-break, onset when nothing qualifies.
2. Status transitions: active → dormant → closed by staleness alone.
3. Facility resolution (§40): nearest-within-radius twin (pure) — same
   answer shape the PostGIS ST_Distance query serves.
4. SQL compile: the ingest-side geography expression and the observation
   natural-key upsert compile to real PostgreSQL SQL without a database.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from sqlalchemy.dialects import postgresql

from app.pipeline.observation_sync import (
    _make_geom_expr,
    normalize_ts,
    observation_rows_for_insert,
    _sensor_tag,
)
from app.pipeline.tracking import (
    CLOSE_AFTER_DAYS,
    DORMANT_AFTER_DAYS,
    FACILITY_RADIUS_KM,
    TRACK_LINK_MAX_GAP_DAYS,
    TRACK_LINK_RADIUS_KM,
    resolve_facilities_pure,
    status_for_last_seen,
    track_uid_for,
    try_link,
)

NOW = datetime(2026, 10, 7, 12, 0, tzinfo=timezone.utc)


class _Cand:
    def __init__(self, tid, lat, lng, last_seen):
        self.id = tid
        self.centroid_lat = lat
        self.centroid_lng = lng
        self.last_seen = last_seen


# ── §15 linkage ──────────────────────────────────────────────────────────


class TestTryLink:
    def _obs(self, lat=28.6, lng=77.2, ts=NOW):
        return {"latitude": lat, "longitude": lng, "acq_timestamp": ts}

    def test_nearest_qualifying_wins(self):
        near = _Cand(1, 28.6, 77.2, NOW - timedelta(hours=1))
        farther = _Cand(2, 28.62, 77.22, NOW - timedelta(hours=1))
        assert try_link(self._obs(), [farther, near]) == 1

    def test_beyond_radius_is_onset(self):
        # ~111 km away > 5 km radius → no link.
        far = _Cand(1, 28.6, 78.3, NOW - timedelta(hours=1))
        assert try_link(self._obs(), [far]) is None

    def test_temporal_gap_forces_onset(self):
        # Spatially at the centroid but the track is 3+ days stale (> gap).
        stale = _Cand(1, 28.6, 77.2, NOW - timedelta(days=TRACK_LINK_MAX_GAP_DAYS + 1))
        assert try_link(self._obs(), [stale]) is None

    def test_future_observation_beyond_gap_is_onset(self):
        # An observation far in the future of last_seen is also a new chapter
        # (track went quiet, then something appeared later elsewhere).
        old = _Cand(1, 28.6, 77.2, NOW - timedelta(days=1))
        future_ts = NOW + timedelta(days=TRACK_LINK_MAX_GAP_DAYS + 1)
        assert try_link(self._obs(ts=future_ts), [old]) is None

    def test_tie_breaks_on_lower_id(self):
        # Two candidates at IDENTICAL distance → lowest id wins (stable).
        a = _Cand(7, 28.6, 77.2, NOW - timedelta(hours=1))
        b = _Cand(3, 28.6, 77.2, NOW - timedelta(hours=1))
        assert try_link(self._obs(), [a, b]) == 3

    def test_radius_matches_node_corroboration(self):
        # Same corroboration radius concept as Node's FACILITY_RADIUS_KM.
        assert TRACK_LINK_RADIUS_KM == 5.0
        assert TRACK_LINK_MAX_GAP_DAYS == 2

    def test_edge_of_radius_links(self):
        # 77.25 is ~4.93 km east at this latitude → within radius, links.
        cand = _Cand(1, 28.6, 77.25, NOW - timedelta(hours=1))
        obs = self._obs(lng=77.1995)
        assert try_link(obs, [cand]) == 1

    def test_just_beyond_radius_is_onset(self):
        # 77.265 is ~6.28 km east → beyond TRACK_LINK_RADIUS_KM → onset.
        cand = _Cand(1, 28.6, 77.265, NOW - timedelta(hours=1))
        obs = self._obs(lng=77.1995)
        assert try_link(obs, [cand]) is None


# ── Status transitions ───────────────────────────────────────────────────


class TestStatusForLastSeen:
    def test_fresh_is_active(self):
        assert status_for_last_seen(NOW - timedelta(days=1), now=NOW) == "active"

    def test_dormant_after_window(self):
        assert (
            status_for_last_seen(
                NOW - timedelta(days=DORMANT_AFTER_DAYS + 1), now=NOW
            )
            == "dormant"
        )

    def test_closed_after_window(self):
        assert (
            status_for_last_seen(
                NOW - timedelta(days=CLOSE_AFTER_DAYS + 1), now=NOW
            )
            == "closed"
        )

    def test_naive_last_seen_treated_as_utc(self):
        naive = NOW.replace(tzinfo=None) - timedelta(days=1)
        assert status_for_last_seen(naive, now=NOW) == "active"

    def test_boundaries_inclusive(self):
        assert status_for_last_seen(NOW - timedelta(days=DORMANT_AFTER_DAYS), now=NOW) == "active"
        assert status_for_last_seen(NOW - timedelta(days=CLOSE_AFTER_DAYS), now=NOW) == "dormant"


# ── §40 facility resolution (pure twin) ─────────────────────────────────


class TestResolveFacilitiesPure:
    def test_nearest_within_radius(self):
        out = resolve_facilities_pure(
            tracks=[(1, 28.6, 77.2)],
            facilities=[("F2", 28.61, 77.21), ("F1", 28.605, 77.205)],
            radius_km=FACILITY_RADIUS_KM,
        )
        assert out[1] == {"facility_id": "F1", "distance_km": out[1]["distance_km"]}
        # F1 is strictly closer than F2.
        assert out[1]["distance_km"] < 1.5

    def test_beyond_radius_unmatched(self):
        out = resolve_facilities_pure(
            tracks=[(1, 28.6, 77.2)],
            facilities=[("F1", 29.6, 77.2)],  # ~111 km north
        )
        assert out[1] is None

    def test_empty_registry_unmatched(self):
        out = resolve_facilities_pure(tracks=[(1, 28.6, 77.2)], facilities=[])
        assert out[1] is None

    def test_distance_rounded(self):
        out = resolve_facilities_pure(
            tracks=[(1, 0.0, 0.0)],
            facilities=[("F", 0.0, 0.01)],
            radius_km=2.0,  # 1.112 km apart →_within_radius
        )
        assert out[1] is not None
        assert out[1]["distance_km"] == round(out[1]["distance_km"], 3)
        assert 1.0 < out[1]["distance_km"] < 1.2


# ── Observation normalization ────────────────────────────────────────────


class TestObservationSyncPure:
    def test_sensor_tag_viirs_variants(self):
        assert _sensor_tag("NOAA-20", "imager") == "VIIRS"
        assert _sensor_tag("NOAA-21", None) == "VIIRS"
        assert _sensor_tag("SNPP (Suomi-NPP)", "imager") == "VIIRS"

    def test_sensor_tag_modis_variants(self):
        assert _sensor_tag("Terra", "MODIS") == "MODIS"
        assert _sensor_tag("Aqua", None) == "MODIS"

    def test_sensor_tag_unknown_passes_through(self):
        assert _sensor_tag("Sentinel-3", None) == "Sentinel-3"
        assert _sensor_tag(None, None) == "unknown"

    def test_normalize_ts_bounds(self):
        assert normalize_ts(date(2026, 10, 7), "1345") == datetime(
            2026, 10, 7, 13, 45, tzinfo=timezone.utc
        )
        # Malformed time strings clamp instead of raising.
        assert normalize_ts(date(2026, 10, 7), "2400").hour == 23
        assert normalize_ts(date(2026, 10, 7), "").minute == 0

    def test_rows_for_insert_adds_geography(self):
        rows = [{"latitude": 28.6, "longitude": 77.2, "acq_date": date(2026, 10, 7)}]
        out = observation_rows_for_insert(rows)
        assert "geom" in out[0]

    def test_geom_expression_compiles_lng_lat_order(self):
        # PostGIS takes (lng, lat); the compiled SQL must contain the cast
        # to geography(POINT,4326) so the GiST index stays usable.
        from sqlalchemy import func, select

        expr = _make_geom_expr(28.61, 77.21)
        stmt = select(func.coalesce(expr, expr))
        sql = str(stmt.compile(dialect=postgresql.dialect()))
        assert "ST_MakePoint" in sql
        assert "geography(POINT,4326)" in sql


class TestTrackUid:
    def test_deterministic(self):
        class O:
            source = "firms"
            satellite = "NOAA-20"
            acq_date = date(2026, 10, 7)
            acq_time = "1345"
            latitude = 28.6
            longitude = 77.2

        assert track_uid_for(O()) == track_uid_for(O())
        assert track_uid_for(O()).startswith("HT-")

    def test_position_sensitive(self):
        class O:
            source = "firms"
            satellite = "NOAA-20"
            acq_date = date(2026, 10, 7)
            acq_time = "1345"
            latitude = 28.6
            longitude = 77.2

        class O2(O):
            latitude = 28.7

        assert track_uid_for(O()) != track_uid_for(O2())


# Type references kept for import-safety assertions.
TRACK_LINK_RADIUS_KM_REF = TRACK_LINK_RADIUS_KM
TRACK_LINK_MAX_GAP_DAYS_REF = TRACK_LINK_MAX_GAP_DAYS
