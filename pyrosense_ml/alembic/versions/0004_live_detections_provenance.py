"""Phase B: FIRMS multi-sensor provenance on live_detections (plan §9).

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-06
"""
from alembic import op
import sqlalchemy as sa

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("live_detections", sa.Column("source", sa.String(32), nullable=False, server_default=""))
    op.add_column("live_detections", sa.Column("ingest_batch_id", sa.String(64), nullable=True))
    op.create_index("ix_live_detections_source", "live_detections", ["source"])


def downgrade() -> None:
    op.drop_index("ix_live_detections_source", table_name="live_detections")
    op.drop_column("live_detections", "ingest_batch_id")
    op.drop_column("live_detections", "source")
