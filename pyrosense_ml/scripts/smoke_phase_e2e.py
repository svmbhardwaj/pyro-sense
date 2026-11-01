"""E2E smoke: Phase B ingest → Phase A canonicalize → Phase D tracking.

Runs the three stages the /api/v2/hotspots/nearby canonical path depends on,
against the configured DATABASE_URL. Per-region/per-stage failure isolation
exists upstream; here we want the smoke to stop loudly on the first error.
"""

from __future__ import annotations

import asyncio
import sys


async def main() -> int:
    from app.db.engine import get_session_factory
    from app.data.live import run_ingest_cycle
    from app.pipeline.observation_sync import sync_observations
    from app.pipeline.tracking import run_tracking, resolve_track_facilities
    from datetime import date, timedelta

    print("── ingest (Phase B, FIRMS multi-sensor) ──")
    inserted = await run_ingest_cycle(day_range=2)
    print(f"live_detections inserted: {inserted}")
    if inserted == 0:
        # Zero can legitimately mean no active fires in-range right now; the
        # canonical path still needs material to answer. Tell the operator.
        print("NOTE: no new detections (no active fires in the regions?)")

    factory = get_session_factory()
    async with factory() as session:
        print("── observe (Phase A canonicalization) ──")
        obs = await sync_observations(
            session,
            day_from=date.today() - timedelta(days=3),
            day_to=date.today() - timedelta(days=0),
        )
        await session.commit()
        print(obs)

        print("── track (Phase D linkage + facility resolution) ──")
        linkage = await run_tracking(session)
        await session.commit()
        print(linkage)
        facilities = await resolve_track_facilities(session)
        await session.commit()
        print(facilities)

    from sqlalchemy import text
    async with factory() as session:
        counts = (
            await session.execute(
                text(
                    "SELECT (SELECT count(*) FROM live_detections) AS live, "
                    "(SELECT count(*) FROM hotspot_observations) AS obs, "
                    "(SELECT count(*) FROM hotspot_tracks) AS tracks, "
                    "(SELECT count(*) FROM hotspot_tracks WHERE facility_id IS NOT NULL) AS matched"
                )
            )
        ).mappings().one()
    print("── row counts ──")
    print(dict(counts))
    return 0 if counts["obs"] > 0 or inserted > 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
