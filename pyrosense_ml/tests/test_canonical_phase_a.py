"""Phase A canonical-contract tests — pure Python, no DB required.

Covers:
1. Ontology v2: legacy label mapping (Node rules, MLP classes, GBM classes),
   canonical passthrough, abstention on unknown labels.
2. Contracts: canonical Hotspot object round-trips; risk degrades to
   insufficient_history rather than fabricating numbers; abstention modeled.
3. Canonical DDL: the ORM table definitions carry geography types, GiST
   indexes, idempotency natural keys, and version provenance columns.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.contracts import (
    CONTRACT_VERSION,
    Classification,
    Hotspot,
    NearbyRequest,
    Risk,
)
from app.db import models as db_models
from app.ontology import (
    ONTOLOGY_LABELS,
    ONTOLOGY_VERSION,
    display_label,
    is_canonical,
    normalize_to_ontology,
)


# ── 1. Ontology v2 ────────────────────────────────────────────────────────


class TestOntologyMapping:
    def test_node_rule_labels_map(self):
        assert normalize_to_ontology("industrial_fire") == "industrial_process"
        assert normalize_to_ontology("gas_flare") == "persistent_thermal_source"
        assert normalize_to_ontology("crop_burning") == "agricultural_burning"
        assert normalize_to_ontology("mining_activity") == "mining_extraction"
        assert normalize_to_ontology("wildfire") == "forest_vegetation"
        assert normalize_to_ontology("persistent_source") == "persistent_thermal_source"

    def test_node_rule_display_strings_map(self):
        # classificationService produces display strings like "Industrial Fire"
        assert normalize_to_ontology("Industrial Fire") == "industrial_process"
        assert normalize_to_ontology("GAS FLARE") == "persistent_thermal_source"
        assert normalize_to_ontology("crop-burning") == "agricultural_burning"

    def test_mlp_training_classes_map(self):
        assert normalize_to_ontology("Agricultural") == "agricultural_burning"
        assert normalize_to_ontology("Industrial") == "industrial_process"
        assert normalize_to_ontology("Forest_Vegetation") == "forest_vegetation"
        assert normalize_to_ontology("Infrastructure_Energy") == "energy_infrastructure"
        assert normalize_to_ontology("Mining") == "mining_extraction"

    def test_canonical_labels_pass_through(self):
        for label in ONTOLOGY_LABELS:
            assert normalize_to_ontology(label) == label
            assert is_canonical(label)

    def test_unknown_maps_to_unknown_not_error(self):
        # Abstention, not a crash (plan §27)
        assert normalize_to_ontology("alien_laser") == "unknown"
        assert normalize_to_ontology("") == "unknown"
        assert normalize_to_ontology(None) == "unknown"

    def test_display_label_always_returns_string(self):
        for label in ONTOLOGY_LABELS:
            assert isinstance(display_label(label), str) and display_label(label)


# ── 2. Contracts ──────────────────────────────────────────────────────────


class TestContracts:
    def _classification(self, **kw):
        return Classification(
            label="industrial_process",
            confidence=0.94,
            model_version="1.0.0",
            ontology_version=ONTOLOGY_VERSION,
            **kw,
        )

    def test_canonical_hotspot_roundtrip(self):
        h = Hotspot(
            id="HS-2026-001034",
            status="active",
            latitude=28.6139,
            longitude=77.2090,
            first_seen="2026-09-01T00:00:00Z",
            last_seen="2026-10-04T00:00:00Z",
            observation_count=27,
            distance_km=13.4,
            classification=self._classification(),
            risk=Risk(risk_1d=0.82, risk_3d=0.73, risk_7d=0.51, status="ok"),
            facility=None,
        )
        dumped = h.model_dump(mode="json")
        assert dumped["classification"]["label"] == "industrial_process"
        assert dumped["risk"]["risk_1d"] == 0.82
        assert dumped["contract_version"] == CONTRACT_VERSION

    def test_risk_insufficient_history_degrades_gracefully(self):
        # Plan §5: cold coordinate -> status, NOT fabricated numbers
        r = Risk(status="insufficient_history")
        assert r.risk_1d is None and r.risk_3d is None and r.risk_7d is None
        dumped = r.model_dump(mode="json")
        assert dumped["status"] == "insufficient_history"

    def test_risk_rejects_probability_out_of_range(self):
        with pytest.raises(ValidationError):
            Risk(risk_1d=1.5, status="ok")

    def test_classification_rejects_non_ontology_label(self):
        with pytest.raises(ValidationError):
            Classification(
                label="alien_laser",  # type: ignore[arg-type]
                confidence=0.9,
                model_version="1.0.0",
                ontology_version=ONTOLOGY_VERSION,
            )

    def test_abstention_modeled(self):
        c = Classification(
            label="unknown",
            confidence=0.41,
            abstained=True,
            abstain_reason="low_confidence",
            model_version="1.0.0",
            ontology_version=ONTOLOGY_VERSION,
        )
        assert c.abstained and c.abstain_reason == "low_confidence"

    def test_nearby_request_bounds(self):
        req = NearbyRequest(latitude=28.6139, longitude=77.2090)
        assert req.radius_km == 50.0
        with pytest.raises(ValidationError):
            NearbyRequest(latitude=999, longitude=0)
        with pytest.raises(ValidationError):
            NearbyRequest(latitude=0, longitude=0, radius_km=0)


# ── 3. Canonical DDL structure (ORM-level, no live DB) ────────────────────


class TestCanonicalDDL:
    def _table(self, name: str):
        return db_models.Base.metadata.tables[name]

    def test_canonical_tables_exist(self):
        names = set(db_models.Base.metadata.tables)
        assert {
            "hotspot_observations",
            "hotspot_tracks",
            "hotspot_predictions",
            "model_version_registry",
        } <= names

    def test_observations_has_geography_and_gist(self):
        from geoalchemy2 import Geography

        t = self._table("hotspot_observations")
        geom = t.c.geom
        assert isinstance(geom.type, Geography)
        assert geom.type.srid == 4326
        idx_names = {i.name for i in t.indexes}
        assert "ix_hotspot_observations_geom_gist" in idx_names
        gist = next(i for i in t.indexes if i.name == "ix_hotspot_observations_geom_gist")
        assert "gist" in (gist.dialect_options["postgresql"]["using"] or "").lower()

    def test_tracks_centroid_gist(self):
        t = self._table("hotspot_tracks")
        idx_names = {i.name for i in t.indexes}
        assert "ix_hotspot_tracks_centroid_geom_gist" in idx_names

    def test_observation_natural_key_idempotency(self):
        # plan §48: re-running ingestion must not duplicate
        t = self._table("hotspot_observations")
        uqs = {c.name for c in t.constraints if c.__class__.__name__ == "UniqueConstraint"}
        assert "uq_hotspot_observations_natural_key" in uqs

    def test_prediction_carries_version_provenance(self):
        t = self._table("hotspot_predictions")
        cols = set(t.columns.keys())
        for col in (
            "ontology_version",
            "model_version",
            "feature_schema_version",
            "dataset_version",
        ):
            assert col in cols, f"missing provenance column {col}"

    def test_prediction_separates_classification_severity_risk(self):
        # plan §5: three distinct concepts, distinct columns
        t = self._table("hotspot_predictions")
        cols = set(t.columns.keys())
        assert {"predicted_label", "confidence", "severity_score", "risk_1d", "risk_3d", "risk_7d"} <= cols

    def test_version_registry_shape(self):
        t = self._table("model_version_registry")
        cols = set(t.columns.keys())
        assert {"component", "version", "is_current", "is_deprecated", "metadata_json"} <= cols

    def test_orm_mapper_configuration_completes(self):
        """configure_mappers() traverses every relationship/back_populates pair.

        Regression guard (Phase C): HotspotTrack.observations once declared
        back_populates="track", which resolved to HotspotObservation's FIRMS
        azimuth *column* (also named `track`) and would have raised
        InvalidRequestError on the first ORM select. Mapper configuration is
        lazy, so import alone never caught it — configure eagerly here.
        """
        from sqlalchemy.orm import configure_mappers

        configure_mappers()
        # The one-way relationship resolved to a real relationship, not the
        # azimuth column.
        assert "observations" in db_models.HotspotTrack.__mapper__.relationships

    def test_migration_0003_head_chains_from_0002(self):
        import importlib.util
        from pathlib import Path

        migration_path = (
            Path(__file__).resolve().parent.parent
            / "alembic"
            / "versions"
            / "0003_canonical_entities.py"
        )
        assert migration_path.exists(), "migration 0003 missing"
        spec = importlib.util.spec_from_file_location("migration_0003", migration_path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)  # type: ignore[union-attr]
        assert mod.revision == "0003"
        assert mod.down_revision == "0002"
