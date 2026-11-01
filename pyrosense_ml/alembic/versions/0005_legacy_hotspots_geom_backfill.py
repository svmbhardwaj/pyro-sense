"""Phase C fallback correctness: backfill hotspots.geom (plan §10, §12).

The legacy-fallback nearby search guards every row on `geom IS NOT NULL`
(exact PostGIS ST_DWithin — the browser never computes distances, plan
P0.1). But the seed and the live-ensure paths wrote only latitude/longitude,
so every seeded row carried a NULL geom and the fallback silently answered
zero results forever.

Idempotent data backfill: geography is a pure derivation of the (lng, lat)
columns (GeoJSON order — plan §8), so re-running is a no-op and existing
non-NULL geoms are untouched.

Downgrade is a no-op by design: the columns remain the source of truth and
cannot distinguish "pre-backfill NULL" from "live row not yet computed".

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-13
"""

from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        UPDATE hotspots
        SET geom = ST_SetSRID(ST_MakePoint(longitude, latitude), 4326)::geography
        WHERE geom IS NULL
          AND latitude IS NOT NULL
          AND longitude IS NOT NULL
            AND latitude BETWEEN -90 AND 90
            AND longitude BETWEEN -180 AND 180;
        """
    )


def downgrade() -> None:
    # Intentionally empty — see module docstring.
    pass
