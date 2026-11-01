"""Phase A canonical entities: hotspot_observations, hotspot_tracks,
hotspot_predictions, model_version_registry — with PostGIS geography columns
and GiST spatial indexes (plan §10, §12).

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-05
"""
from alembic import op
import sqlalchemy as sa
from geoalchemy2 import Geography
from sqlalchemy.dialects import postgresql

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS postgis")

    # ── hotspot_tracks (referenced by observations) ────────────────────────
    op.create_table(
        "hotspot_tracks",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("track_uid", sa.String(64), nullable=False, unique=True),
        sa.Column("status", sa.String(8), nullable=False),
        sa.Column("centroid_lat", sa.Float(), nullable=False),
        sa.Column("centroid_lng", sa.Float(), nullable=False),
        # PostGIS geography — declared here, matching the ORM model (models.py);
        # a JSONB placeholder here makes the later geography ALTER a silent
        # no-op and the GiST index fail (jsonb has no GIST point opclass).
        sa.Column("centroid_geom", Geography(geometry_type="POINT", srid=4326), nullable=True),
        sa.Column("radius_km", sa.Float(), nullable=False),
        sa.Column("first_seen", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen", sa.DateTime(timezone=True), nullable=False),
        sa.Column("observation_count", sa.Integer(), nullable=False),
        sa.Column("unique_fire_days", sa.Integer(), nullable=False),
        sa.Column("cells", postgresql.JSONB(), nullable=False),
        sa.Column("persistence_score", sa.Float(), nullable=False),
        sa.Column("sensor_agreement", sa.Integer(), nullable=False),
        sa.Column("facility_match_type", sa.String(32), nullable=True),
        sa.Column("facility_id", sa.String(128), nullable=True),
        sa.Column("facility_distance_km", sa.Float(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_hotspot_tracks_last_seen", "hotspot_tracks", ["last_seen"])
    op.create_index("ix_hotspot_tracks_facility_id", "hotspot_tracks", ["facility_id"])
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_hotspot_tracks_centroid_geom_gist "
        "ON hotspot_tracks USING GIST (centroid_geom);"
    )

    # ── hotspot_observations ───────────────────────────────────────────────
    op.create_table(
        "hotspot_observations",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("latitude", sa.Float(), nullable=False),
        sa.Column("longitude", sa.Float(), nullable=False),
        sa.Column("geom", Geography(geometry_type="POINT", srid=4326), nullable=True),
        sa.Column("acq_date", sa.Date(), nullable=False),
        sa.Column("acq_time", sa.String(8), nullable=False),
        sa.Column("acq_timestamp", sa.DateTime(timezone=True), nullable=False),
        sa.Column("sensor", sa.String(16), nullable=False),
        sa.Column("satellite", sa.String(32), nullable=False),
        sa.Column("instrument", sa.String(32), nullable=True),
        sa.Column("source", sa.String(16), nullable=False),
        sa.Column("ingest_batch_id", sa.String(64), nullable=True),
        sa.Column("frp", sa.Float(), nullable=False),
        sa.Column("brightness_temp", sa.Float(), nullable=True),
        sa.Column("brightness_temp2", sa.Float(), nullable=True),
        sa.Column("confidence", sa.String(16), nullable=True),
        sa.Column("scan", sa.Float(), nullable=True),
        sa.Column("track", sa.Float(), nullable=True),
        sa.Column("daynight", sa.String(8), nullable=True),
        sa.Column("region", sa.String(64), nullable=True),
        sa.Column(
            "track_id",
            sa.Integer(),
            sa.ForeignKey("hotspot_tracks.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("ingested_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint(
            "source", "satellite", "acq_date", "acq_time", "latitude", "longitude",
            name="uq_hotspot_observations_natural_key",
        ),
    )
    op.create_index("ix_hotspot_observations_track_id", "hotspot_observations", ["track_id"])
    op.create_index("ix_hotspot_observations_acq", "hotspot_observations", ["acq_date", "acq_time"])
    op.create_index("ix_hotspot_observations_ingest_batch_id", "hotspot_observations", ["ingest_batch_id"])
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_hotspot_observations_geom_gist "
        "ON hotspot_observations USING GIST (geom);"
    )

    # ── hotspot_predictions ────────────────────────────────────────────────
    op.create_table(
        "hotspot_predictions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("subject_type", sa.String(12), nullable=False),
        sa.Column(
            "track_id",
            sa.Integer(),
            sa.ForeignKey("hotspot_tracks.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column(
            "observation_id",
            sa.Integer(),
            sa.ForeignKey("hotspot_observations.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("h3_cell", sa.String(32), nullable=True),
        sa.Column("predicted_label", sa.String(48), nullable=False),
        sa.Column("ontology_version", sa.String(8), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("uncertainty", sa.Float(), nullable=True),
        sa.Column("abstained", sa.Boolean(), nullable=False),
        sa.Column("abstain_reason", sa.String(64), nullable=True),
        sa.Column("probabilities", postgresql.JSONB(), nullable=False),
        sa.Column("severity", sa.String(8), nullable=True),
        sa.Column("severity_score", sa.Float(), nullable=True),
        sa.Column("risk_1d", sa.Float(), nullable=True),
        sa.Column("risk_3d", sa.Float(), nullable=True),
        sa.Column("risk_7d", sa.Float(), nullable=True),
        sa.Column("risk_status", sa.String(24), nullable=False),
        sa.Column("model_version", sa.String(32), nullable=False),
        sa.Column("feature_schema_version", sa.String(32), nullable=False),
        sa.Column("dataset_version", sa.String(32), nullable=False),
        sa.Column("feature_snapshot", postgresql.JSONB(), nullable=False),
        sa.Column("evidence", postgresql.JSONB(), nullable=False),
        sa.Column("predicted_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_hotspot_predictions_subject_time", "hotspot_predictions",
                    ["subject_type", "track_id", "observation_id", "predicted_at"])
    op.create_index("ix_hotspot_predictions_label", "hotspot_predictions", ["predicted_label"])
    op.create_index("ix_hotspot_predictions_track_id", "hotspot_predictions", ["track_id"])
    op.create_index("ix_hotspot_predictions_observation_id", "hotspot_predictions", ["observation_id"])
    op.create_index("ix_hotspot_predictions_h3_cell", "hotspot_predictions", ["h3_cell"])

    # ── model_version_registry ─────────────────────────────────────────────
    op.create_table(
        "model_version_registry",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("component", sa.String(32), nullable=False),
        sa.Column("version", sa.String(32), nullable=False),
        sa.Column("is_current", sa.Boolean(), nullable=False),
        sa.Column("is_deprecated", sa.Boolean(), nullable=False),
        sa.Column("metadata_json", postgresql.JSONB(), nullable=False),
        sa.Column("registered_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("component", "version", name="uq_model_version_registry_component_version"),
    )
    op.create_index("ix_model_version_registry_is_current", "model_version_registry", ["is_current"])
    op.create_index("ix_model_version_registry_current", "model_version_registry", ["component", "is_current"])

    # Seed the registry with the versions currently in force (plan §24).
    op.execute(
        """
        INSERT INTO model_version_registry (component, version, is_current, is_deprecated, metadata_json)
        VALUES
            ('classifier', '1.0.0', true, false, '{"family": "mlp"}'),
            ('risk', '1.0.0', true, false, '{"family": "gru-3h"}'),
            ('ontology', '2', true, false, '{"labels": 7}'),
            ('feature_schema', '2.0.0', true, false, '{"classifier_features": 43, "risk_features": 17}'),
            ('dataset', '1.0.0', true, false, '{}')
        ON CONFLICT (component, version) DO NOTHING;
        """
    )


def downgrade() -> None:
    op.drop_table("model_version_registry")
    op.drop_table("hotspot_predictions")
    op.drop_table("hotspot_observations")
    op.drop_table("hotspot_tracks")
