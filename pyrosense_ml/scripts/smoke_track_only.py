"""Torpedo smoke step: run_tracking + resolve_track_facilities with timing."""

import asyncio
import sys
import time


async def main() -> int:
    from app.db.engine import get_session_factory
    from app.pipeline.tracking import run_tracking, resolve_track_facilities

    t0 = time.monotonic()
    factory = get_session_factory()
    async with factory() as session:
        linkage = await run_tracking(session)
        await session.commit()
        print(f"linkage ({time.monotonic()-t0:.1f}s): {linkage}")

        t1 = time.monotonic()
        facilities = await resolve_track_facilities(session)
        await session.commit()
        print(f"facilities ({time.monotonic()-t1:.1f}s): {facilities}")

    from sqlalchemy import text
    async with factory() as session:
        c = (
            await session.execute(
                text("SELECT count(*) FROM hotspot_tracks WHERE track_uid IS NOT NULL")
            )
        ).scalar_one()
    print(f"tracks committed: {c}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
