"use client";

/**
 * PYROSENSE — /api/v2/hotspots/nearby client (plan P0.1, Phase E).
 *
 * Transport is ALWAYS the Node backend's pass-through proxy
 * (GET /api/v2/hotspots/nearby → pyrosense_ml's PostGIS endpoint, v2.route.ts).
 * The browser NEVER computes the authoritative nearby list — it sends a
 * coordinate and the database answers with exact geography distances
 * (ST_DWithin / ST_Distance). The client-side Haversine helpers in lib/geo.ts
 * stay for display-only purposes (e.g. "km from pinned point" labels), never
 * as the authority for what is nearby.
 *
 * The DTOs mirror pyrosense_ml's NearbyResponseBody verbatim
 * (app/api/nearby.py + app/data/spatial.py::NearbyResult.to_dict) — the
 * backend proxies pass-through, so this is the single definition.
 */

import { API_BASE } from "./api";

/* ------------------------------------------------------------------ */
/* DTOs (mirror app/api/nearby.py NearbyResponseBody)                   */
/* ------------------------------------------------------------------ */

/** Ontology v2 label + model provenance (contract-shaped, may be null on rows without a prediction). */
export interface NearbyClassificationDto {
  label: string;
  confidence: number | null;
  uncertainty: number | null;
  abstained: boolean;
  abstain_reason: string | null;
  model_version: string;
  ontology_version: string;
}

/**
 * GRU temporal risk block. `status: "insufficient_history"` (or all-null
 * horizons) means the temporal model has not scored this track — rendered
 * as "not scored", never coerced to 0/LOW.
 */
export interface NearbyRiskDto {
  risk_1d: number | null;
  risk_3d: number | null;
  risk_7d: number | null;
  status: string;
  model_version?: string;
}

/**
 * Legacy rows expose their single risk_score as SEVERITY, never dressed up
 * as 1/3/7-day risk (plan §5). Canonical tracks omit this field.
 */
export interface NearbySeverityDto {
  score: number | null;
  level: "HIGH" | "MODERATE" | "LOW";
}

/** Facility match (Phase D tracking): null when the track matched no registered facility. */
export interface NearbyFacilityDto {
  facility_id: string;
  name: string | null;
  match_type: string;
  distance_km: number | null;
}

/** One nearby hit — canonical NearbyResult shape (results list is free of invented fields). */
export interface NearbyResultDto {
  hotspot_id: string;
  latitude: number;
  longitude: number;
  distance_km: number;
  status: string;
  first_seen: string | null;
  last_seen: string | null;
  observation_count: number;
  classification: NearbyClassificationDto | null;
  risk: NearbyRiskDto | null;
  severity: NearbySeverityDto | null;
  facility: NearbyFacilityDto | null;
  evidence: unknown[];
  contract_version: string;
}

/**
 * Which store served the answer: "hotspot_tracks" (canonical) or
 * "legacy_hotspots" (fallback while the tracking pipeline has not yet run).
 * Surfaced in the UI so fallback data is never mistaken for canonical data.
 */
export type NearbySource = "hotspot_tracks" | "legacy_hotspots";

export interface NearbyMetaDto {
  source: NearbySource;
  fallback_used: boolean;
  results_count: number;
}

export interface NearbyResponseDto {
  center: { latitude: number; longitude: number };
  radius_km: number;
  results: NearbyResultDto[];
  meta: NearbyMetaDto;
  contract_version: string;
}

/* ------------------------------------------------------------------ */
/* fetcher                                                             */
/* ------------------------------------------------------------------ */

export class NearbyApiError extends Error {
  constructor(
    public readonly status: number,
    message: string,
  ) {
    super(message);
    this.name = "NearbyApiError";
  }
}

/**
 * GET /api/v2/hotspots/nearby?lat=…&lng=… — server-authoritative nearby
 * search. The BFF validates + short-caches (30 s LRU) and returns its own
 * 503 with a hint when the ML service is unreachable — that 503 is the
 * caller's signal to degrade honestly (§74 Scenario E), never to fake data.
 */
export async function fetchNearbyHotspots(
  lat: number,
  lng: number,
  opts?: { radiusKm?: number; limit?: number; sinceDays?: number },
): Promise<NearbyResponseDto> {
  const qs = new URLSearchParams({
    lat: String(lat),
    lng: String(lng),
  });
  if (opts?.radiusKm != null) qs.set("radius_km", String(opts.radiusKm));
  if (opts?.limit != null) qs.set("limit", String(opts.limit));
  if (opts?.sinceDays != null) qs.set("since_days", String(opts.sinceDays));

  const res = await fetch(`${API_BASE}/api/v2/hotspots/nearby?${qs.toString()}`);
  if (!res.ok) {
    let detail = `API ${res.status} for /api/v2/hotspots/nearby`;
    try {
      const body = (await res.json()) as { error?: string; hint?: string };
      if (typeof body.error === "string") detail = body.error;
      if (typeof body.hint === "string") detail += ` (${body.hint})`;
    } catch {
      /* keep the generic detail */
    }
    throw new NearbyApiError(res.status, detail);
  }
  return (await res.json()) as NearbyResponseDto;
}
