"""Phase B multi-sensor FIRMS tests — pure Python, no network, no DB.

Mirrors backend/src/services/firmsClient.test.ts so the two services
share the exact same sensor-resolution semantics (plan §9).
"""

from __future__ import annotations

from datetime import date

from app.data.live import (
    FIRMS_SENSOR_SOURCES,
    SNPP_END_OF_LIFE,
    active_firms_sources,
    parse_firms_csv,
)


VIIRS_CSV = (
    "latitude,longitude,bright_ti4,bright_ti5,frp,acq_date,acq_time,satellite,instrument,confidence,daynight\n"
    "28.61,77.20,344.1,301.2,7.5,2026-10-05,1345,N20,VIIRS,90,D\n"
    "28.72,77.10,340.0,299.0,5.0,2026-10-05,1345,N20,VIIRS,85,N\n"
)


class TestActiveSources:
    def test_registr(self):
        assert set(FIRMS_SENSOR_SOURCES) == {"NOAA20", "NOAA21", "SNPP"}
        assert SNPP_END_OF_LIFE == "2026-11-01"

    def test_snpp_active_before_eol(self):
        sources = [s["source"] for s in active_firms_sources(date(2026, 10, 31))]
        assert set(sources) == {"VIIRS_NOAA20_NRT", "VIIRS_NOAA21_NRT", "VIIRS_SNPP_NRT"}

    def test_snpp_drops_out_on_eol_date_itself(self):
        # "Processing ceases Nov 1" — inclusive drop.
        sources = [s["source"] for s in active_firms_sources(date(2026, 11, 1))]
        assert set(sources) == {"VIIRS_NOAA20_NRT", "VIIRS_NOAA21_NRT"}

        later = [s["source"] for s in active_firms_sources(date(2027, 1, 1))]
        assert set(later) == {"VIIRS_NOAA20_NRT", "VIIRS_NOAA21_NRT"}

    def test_specs_carry_provenance_metadata(self):
        spec = next(s for s in active_firms_sources(date(2026, 10, 1)) if s["satellite"] == "NOAA-21")
        assert spec["source"] == "VIIRS_NOAA21_NRT"
        assert spec["instrument"] == "VIIRS"


class TestParseProvenance:
    def test_rows_tagged_with_source(self):
        rows = parse_firms_csv(VIIRS_CSV, source="VIIRS_NOAA20_NRT")
        assert len(rows) == 2
        assert all(r["source"] == "VIIRS_NOAA20_NRT" for r in rows)
        assert rows[0]["satellite"] == "N20"

    def test_rows_default_source_empty(self):
        rows = parse_firms_csv(VIIRS_CSV)
        assert all(r["source"] == "" for r in rows)

    def test_malformed_rows_skipped(self):
        bad = "latitude,longitude,frp,acq_date,acq_time\n28.5,77.2,1.0,2026-10-05,1345\nx,y,z,a,b\n"
        rows = parse_firms_csv(bad)
        assert len(rows) == 1

    def test_dedupe_key_includes_source(self):
        # store_detections dedupes on lat/lng/date/time/satellite/source —
        # different sources with same satellite string must NOT collide.
        from app.data.live import parse_firms_csv as p

        a = p(VIIRS_CSV, source="VIIRS_NOAA20_NRT")[0]
        b = p(VIIRS_CSV, source="VIIRS_NOAA21_NRT")[0]
        assert a["source"] != b["source"]
        keys = {
            (r["latitude"], r["longitude"], r["acq_date"], r["acq_time"], r["satellite"], r["source"])
            for r in (a, b)
        }
        assert len(keys) == 2
