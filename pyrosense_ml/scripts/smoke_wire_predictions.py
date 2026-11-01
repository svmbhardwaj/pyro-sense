"""Torpedo smoke: wire the GRU + track-prediction paths against the live DB.

1. Seed ONE cell's 30-day window in firms_daily_h3 (flagged SYNTHETIC in
   fill_applied so it is auditable — this exists only to give the GRU a
   rankable sequence in a smoke DB that otherwise has ~2 days of history).
2. Run the risk pipeline (reads that cell → risk_predictions).
3. Run the capped track-predictions stage (classify a couple of tracks,
   attach stored GRU risk as canonical HotspotPrediction rows).
"""

import asyncio
import sys
import time
from datetime import date, timedelta

import h3
from sqlalchemy import text


async def main() -> int:
    from app.db.engine import get_session_factory
    from app.ml.model_loader import load_model
    from app.pipeline.risk_batch import run_risk_pipeline
    from app.pipeline.track_predictions import run_track_predictions_stage

    # Standalone process: normal startup loads models in the lifespan hook.
    load_model()

    factory = get_session_factory()

    async with factory() as session:
        row = (
            await session.execute(
                text(
                    "SELECT id, track_uid, centroid_lat, centroid_lng, status "
                    "FROM hotspot_tracks WHERE status IN ('active','dormant') "
                    "ORDER BY last_seen DESC LIMIT 1"
                )
            )
        ).one()
        track_id, track_uid, lat, lng, status = row
        cell = h3.latlng_to_cell(float(lat), float(lng), 7)
        print(f"target track {track_uid} ({status}) @ {float(lat):.4f},{float(lng):.4f} cell={cell}")

        as_of = date.today() - timedelta(days=1)
        synthetic = [
            {
                "h3_cell": cell,
                "day": as_of - timedelta(days=i),
                "fire_count": 3,
                "contributing_detections": 3,
                "feature_vector": [0.5] * 17,
                "fill_applied": {"note": "SYNTHETIC smoke_wire_predictions"},
            }
            for i in range(30)
        ]
        from sqlalchemy.dialects.postgresql import insert as pg_insert
        from app.db.models import FirmsDailyH3

        stmt = pg_insert(FirmsDailyH3).values(synthetic)
        stmt = stmt.on_conflict_do_nothing(constraint="uq_firms_daily_h3_cell_day")
        await session.execute(stmt)
        await session.commit()
        print(f"seeded 30 synthetic feature rows for {cell} (as_of={as_of})")

    t0 = time.monotonic()
    async with factory() as session:
        r = await run_risk_pipeline(session)
        await session.commit()
        print(
            f"risk stage ({time.monotonic()-t0:.1f}s): eligible={r.cells_eligible} "
            f"insufficient={r.cells_insufficient} rows={r.rows_upserted} as_of={r.as_of}"
        )

    t1 = time.monotonic()
    async with factory() as session:
        s = await run_track_predictions_stage(session, limit=3)
        await session.commit()
        print(
            f"track-predictions stage ({time.monotonic()-t1:.1f}s): {s}"
        )

    async with factory() as session:
        risks = (await session.execute(text("SELECT h3_cell, horizon, round(probability::numeric,3), level, overall FROM risk_predictions"))).all()
        preds = (await session.execute(text("SELECT count(*), min(track_id), max(risk_status) FROM hotspot_predictions WHERE subject_type='track'"))).one()
        print("risk rows:", risks)
        print("canonical track predictions:", preds)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
