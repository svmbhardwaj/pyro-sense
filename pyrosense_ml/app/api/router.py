"""API router aggregator."""

from __future__ import annotations

from fastapi import APIRouter

from app.api import health, hotspots, internal, nearby, observations, predict

api_router = APIRouter()
api_router.include_router(health.router)
api_router.include_router(hotspots.router)
api_router.include_router(predict.router)
api_router.include_router(nearby.router)  # Phase C: server-authoritative 50 km search (plan P0.1)
# Display-facing per-point observations (no inference, nothing persisted).
api_router.include_router(observations.router)
# Phase 3: internal ML surface — Node BFF only, never the browser.
api_router.include_router(internal.router)
