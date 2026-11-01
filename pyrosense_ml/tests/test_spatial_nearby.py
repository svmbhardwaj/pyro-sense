"""Phase C spatial-layer tests — pure Python, no live PostGIS required.

1. SQL compile checks: both nearby statements compile to PostgreSQL SQL.
   This is the no-database equivalent of an EXPLAIN — it catches bad
   geoalchemy casts, missing columns, and broken joins the way a runtime
   query would, without needing a PostGIS server.
2. Contract shape: NearbyResult.to_dict() matches app/contracts.py's
   nearby hit shape exactly (§52), with severity and temporal risk kept
   separate (§5) and legacy risk_scores degrading to insufficient_history.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy.dialects import postgresql

from app.contracts import CONTRACT_VERSION
from app.data.spatial import NearbyResult, _nearby_legacy_stmt, _nearby_tracks_stmt


class TestNearbySQLCompilation:
    def test_tracks_statement_compiles_with_geography_cast(self):
        stmt = _nearby_tracks_stmt(
            lat=28.61,
            lng=77.21,
            radius_m=50_000.0,
            since=datetime.now(timezone.utc) - timedelta(days=7),
            limit=25,
        )
        sql = str(stmt.compile(dialect=postgresql.dialect()))
        assert "ST_DWithin" in sql, sql
        # The WGS84 point is cast to geography — the canonical distance space.
        # geoalchemy2 renders cast() as CAST(x AS geography(POINT,4326)).
        assert "AS geography(POINT,4326)" in sql, sql
        assert "hotspot_tracks" in sql
        # Ordered by true distance, not bounding-box approximation.
        assert "ORDER BY dist_m" in sql
        # Closed tracks excluded; recency window applied.
        assert "status" in sql
        assert "last_seen" in sql

    def test_legacy_statement_compiles_with_geography_cast(self):
        stmt = _nearby_legacy_stmt(lat=28.61, lng=77.21, radius_m=50_000.0, limit=25)
        sql = str(stmt.compile(dialect=postgresql.dialect()))
        assert "ST_DWithin" in sql, sql
        assert "AS geography(POINT,4326)" in sql, sql
        assert "hotspots " in sql or "hotspots\n" in sql or sql.count("hotspots") >= 1
        assert "ORDER BY dist_m" in sql

    def test_point_is_lng_lat_order(self):
        # PostGIS takes (lng, lat); a swapped build would silently search the
        # wrong hemisphere. The SQL literally embeds the constants — check.
        stmt = _nearby_tracks_stmt(
            lat=28.61,
            lng=77.21,
            radius_m=10_000.0,
            since=datetime.now(timezone.utc),
            limit=1,
        )
        sql = str(stmt.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))
        # ST_SetSRID(ST_MakePoint(lng, lat), 4326): lng (77.21) BEFORE lat (28.61).
        assert "ST_MakePoint(77.21, 28.61)" in sql, sql


class TestNearbyResultShape:
    def _result(self, **overrides) -> NearbyResult:
        base = dict(
            hotspot_id="track_abc",
            latitude=28.61,
            longitude=77.21,
            distance_km=12.34567,
            status="active",
            first_seen=datetime(2026, 10, 1, tzinfo=timezone.utc),
            last_seen=datetime(2026, 10, 6, tzinfo=timezone.utc),
            observation_count=42,
            classification={"label": "industrial_process", "confidence": 0.81},
            risk={"risk_1d": 0.4, "risk_3d": None, "risk_7d": None, "status": "ok"},
            severity=None,
            facility=None,
        )
        base.update(overrides)
        return NearbyResult(**base)

    def test_to_dict_keys_match_contract(self):
        d = self._result().to_dict()
        expected = {
            "hotspot_id",
            "latitude",
            "longitude",
            "distance_km",
            "status",
            "first_seen",
            "last_seen",
            "observation_count",
            "classification",
            "risk",
            "severity",
            "facility",
            "evidence",
            "contract_version",
        }
        assert set(d) == expected

    def test_distance_rounded_and_datetimes_iso(self):
        d = self._result().to_dict()
        assert d["distance_km"] == 12.346
        assert d["first_seen"] == "2026-10-01T00:00:00+00:00"
        assert d["last_seen"] == "2026-10-06T00:00:00+00:00"
        assert d["contract_version"] == CONTRACT_VERSION

    def test_legacy_risk_degrades_to_insufficient_history(self):
        d = self._result(
            classification={"label": "unknown", "confidence": None},
            risk={"risk_1d": None, "risk_3d": None, "risk_7d": None, "status": "insufficient_history"},
            severity={"score": 0.72, "level": "HIGH"},
        ).to_dict()
        assert d["risk"]["status"] == "insufficient_history"
        # Severity is separate — legacy risk_score never becomes 1/3/7d risk (§5).
        assert d["severity"] == {"score": 0.72, "level": "HIGH"}
        assert d["risk"]["risk_1d"] is None

    def test_null_datetimes_stay_null(self):
        d = self._result(first_seen=None, last_seen=None).to_dict()
        assert d["first_seen"] is None
        assert d["last_seen"] is None
