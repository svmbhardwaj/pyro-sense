/**
 * v2.route — Phase C/F API surface (plan §52): /api/v2/hotspots/nearby.
 *
 * Server-authoritative 50 km nearby search, proxied pass-through to
 * pyrosense_ml's PostGIS-backed endpoint. The browser never computes the
 * authoritative nearby list (plan P0.1). Short LRU cache (plan §54 level 3)
 * keyed by the rounded query — the same coordinate asked twice within the
 * TTL hits the cache.
 */

import { Router } from "express";
import { LRUCache } from "lru-cache";
import { z } from "zod";
import { proxyNearbyHotspots } from "../services/mlProxyService.js";
import { httpLog } from "../lib/logger.js";

const NearbyQuerySchema = z.object({
  lat: z.coerce.number().min(-90).max(90),
  lng: z.coerce.number().min(-180).max(180),
  radius_km: z.coerce.number().gt(0).max(500).optional().default(50),
  limit: z.coerce.number().int().min(1).max(500).optional().default(50),
  since_days: z.coerce.number().int().min(1).max(365).optional().default(7),
});

const CACHE_TTL_MS = 30_000;
const nearbyCache = new LRUCache<string, Record<string, unknown>>({ max: 500, ttl: CACHE_TTL_MS });

export const v2Router = Router();

v2Router.get("/api/v2/hotspots/nearby", async (req, res) => {
  const parsed = NearbyQuerySchema.safeParse(req.query);
  if (!parsed.success) {
    res.status(400).json({ error: "invalid lat/lng/radius query", issues: parsed.error.issues });
    return;
  }
  const { lat, lng, radius_km, limit, since_days } = parsed.data;

  const cacheKey = `${lat.toFixed(3)},${lng.toFixed(3)},${radius_km},${limit},${since_days}`;
  const cached = nearbyCache.get(cacheKey);
  if (cached) {
    res.json(cached);
    return;
  }

  const qs = `lat=${lat}&lng=${lng}&radius_km=${radius_km}&limit=${limit}&since_days=${since_days}`;
  try {
    const { status, body } = await proxyNearbyHotspots(qs);
    if (status !== 200) {
      res.status(status).json(body);
      return;
    }
    // Pass-through contract, but only cache/forward well-formed JSON objects.
    if (typeof body !== "object" || body === null) {
      res.status(502).json({ error: "nearby search returned malformed payload" });
      return;
    }
    const typed = body as Record<string, unknown>;
    nearbyCache.set(cacheKey, typed);
    res.json(typed);
  } catch (err) {
    httpLog.warn({ err: String(err) }, "nearby proxy failed");
    res.status(503).json({
      error: "nearby search unavailable — ML service unreachable",
      // §74 Scenario E: the UI stays usable; raw observations can still load.
      hint: "hotspot observations remain available via /api/ml/observations",
    });
  }
});
