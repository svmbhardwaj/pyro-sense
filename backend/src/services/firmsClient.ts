/**
 * firmsClient — ALL raw NASA FIRMS API calls live here, nowhere else.
 *
 * Phase B (plan §9): MULTI-SENSOR ingestion. NASA's current FIRMS NRT API
 * serves VIIRS from three satellites; S-NPP processing/distribution ceases
 * 2026-11-01, so production is NOAA-20 + NOAA-21 with S-NPP as a
 * transitional source that drops out automatically after end-of-life.
 *
 * Two call shapes (both verified against the live API):
 *   1. Recency window:  /{key}/{dataset}/{bbox}/{dayRange}          dayRange 1..5
 *   2. Dated window:    /{key}/{dataset}/{bbox}/{dayRange}/{endDate} dayRange 1..5
 *
 * NASA hard-caps dayRange at 5 (dayRange 7/10 → "Invalid day range. Expects
 * [1..5]."). Longer history is reached by calling the DATED shape repeatedly
 * with fixed end dates — exactly what the ingestion job does.
 *
 * Every fetch requests EACH active sensor source separately: one sensor's
 * upstream failure degrades to fewer sensors (logged), never to an empty
 * product. Every row carries `source` (the FIRMS dataset id) so provenance
 * survives into the database — never merged away (plan §9, §80).
 */

import { env } from "../config/env.js";
import { FIRMS_MAX_REQUEST_DAYS } from "../config/regions.js";
import { FIRMS_QUEUE, httpRequest } from "../lib/http.js";
import { firmsLog } from "../lib/logger.js";

/** S-NPP processing/distribution ends on this date (NASA announcement). */
export const SNPP_END_OF_LIFE = "2026-11-01";

export interface FirmsSensorSpec {
  /** FIRMS dataset id — the path segment in the Area API URL. */
  source: string;
  /** Physical sensor family. */
  sensor: "VIIRS" | "MODIS";
  /** Canonical satellite name persisted alongside detections. */
  satellite: string;
  instrument: string;
  lifecycle: "primary" | "transitional";
  /** ISO date (YYYY-MM-DD) after which the source is no longer requested. */
  endOfLife?: string;
}

/** The VIIRS NRT sources PyroSense understands (plan §9 target topology). */
export const FIRMS_SENSOR_REGISTRY = {
  NOAA20: {
    source: "VIIRS_NOAA20_NRT",
    sensor: "VIIRS",
    satellite: "NOAA-20",
    instrument: "VIIRS",
    lifecycle: "primary",
    endOfLife: undefined,
  },
  NOAA21: {
    source: "VIIRS_NOAA21_NRT",
    sensor: "VIIRS",
    satellite: "NOAA-21",
    instrument: "VIIRS",
    lifecycle: "primary",
    endOfLife: undefined,
  },
  SNPP: {
    source: "VIIRS_SNPP_NRT",
    sensor: "VIIRS",
    satellite: "S-NPP",
    instrument: "VIIRS",
    lifecycle: "transitional",
    endOfLife: SNPP_END_OF_LIFE,
  },
} as const satisfies Record<string, FirmsSensorSpec>;

export type FirmsSensorKey = keyof typeof FIRMS_SENSOR_REGISTRY;

export const ALL_SENSOR_KEYS = Object.keys(FIRMS_SENSOR_REGISTRY) as FirmsSensorKey[];

/**
 * Active sensor keys for a given moment (UTC). Default: every registered
 * sensor whose end-of-life has not passed. `FIRMS_SENSORS` (comma-separated
 * keys, e.g. "NOAA20,NOAA21") pins an explicit subset for ops; unknown or
 * retired keys are dropped with a warning. An end-of-life date is INCLUSIVE:
 * the sensor is dropped ON that date ("processing ceases Nov 1" ⇒ no data
 * served that day or later).
 */
export function resolveActiveSensorKeys(
  now: Date = new Date(),
  override: string | undefined = env.FIRMS_SENSORS,
): FirmsSensorKey[] {
  const configured = override?.trim();
  if (!configured) {
    return ALL_SENSOR_KEYS.filter((k) => {
      const eol = FIRMS_SENSOR_REGISTRY[k].endOfLife;
      return !eol || now.toISOString().slice(0, 10) < eol;
    });
  }
  const requested = configured
    .split(",")
    .map((s) => s.trim().toUpperCase())
    .filter(Boolean);
  const valid = requested.filter((k) => k in FIRMS_SENSOR_REGISTRY) as FirmsSensorKey[];
  if (valid.length !== requested.length) {
    firmsLog.warn(
      { requested, known: ALL_SENSOR_KEYS },
      "FIRMS_SENSORS contains unknown keys — ignoring them",
    );
  }
  return valid;
}

/**
 * One raw VIIRS detection, exactly as FIRMS CSV columns name them, plus the
 * `source` dataset id it was fetched from (provenance — plan §9).
 */
export interface FirmsCsvRow {
  latitude: number;
  longitude: number;
  bright_ti4: number | null;
  bright_ti5: number | null;
  scan: number | null;
  track: number | null;
  frp: number;
  acq_date: string; // YYYY-MM-DD
  acq_time: string; // HHMM
  satellite: string;
  instrument: string;
  confidence: string;
  daynight: string;
  /** FIRMS dataset id the row came from, e.g. "VIIRS_NOAA20_NRT". */
  source: string;
}

export function parseCsv(csv: string, source = ""): FirmsCsvRow[] {
  const text = csv.trim();
  // Error payloads are plain text ("Invalid day range..."); real payloads
  // start with the header line containing "latitude".
  if (!text || !text.includes("latitude")) return [];

  const lines = text.split("\n");
  const header = lines[0]!.split(",").map((h) => h.trim());
  const idx = (name: string) => header.indexOf(name);
  const iLat = idx("latitude");
  const iLng = idx("longitude");
  if (iLat < 0 || iLng < 0) return [];

  const rows: FirmsCsvRow[] = [];
  for (let i = 1; i < lines.length; i++) {
    const cols = lines[i]!.split(",");
    if (cols.length < header.length) continue;
    const lat = parseFloat(cols[iLat]!);
    const lng = parseFloat(cols[iLng]!);
    if (!Number.isFinite(lat) || !Number.isFinite(lng)) continue;
    const num = (s: string | undefined): number | null => {
      const n = s === undefined ? NaN : parseFloat(s);
      return Number.isFinite(n) ? n : null;
    };
    rows.push({
      latitude: lat,
      longitude: lng,
      bright_ti4: num(cols[idx("bright_ti4")]),
      bright_ti5: num(cols[idx("bright_ti5")]),
      scan: num(cols[idx("scan")]),
      track: num(cols[idx("track")]),
      frp: num(cols[idx("frp")]) ?? 0,
      acq_date: (cols[idx("acq_date")] ?? "").trim(),
      acq_time: (cols[idx("acq_time")] ?? "").trim(),
      satellite: (cols[idx("satellite")] ?? "").trim(),
      instrument: (cols[idx("instrument")] ?? "").trim(),
      confidence: (cols[idx("confidence")] ?? "").trim(),
      daynight: (cols[idx("daynight")] ?? "").trim(),
      source,
    });
  }
  return rows;
}

/**
 * In-batch dedupe on the natural key (plan §48). FIRMS payloads per source
 * are unique, but the same physical detection can appear from multiple
 * sources ONLY if NASA ever overlaps datasets — and replays can duplicate
 * within a source. Key: source + satellite + position + acquisition time.
 */
export function dedupeRows(rows: FirmsCsvRow[]): FirmsCsvRow[] {
  const seen = new Set<string>();
  const out: FirmsCsvRow[] = [];
  for (const r of rows) {
    const key = `${r.source}|${r.satellite}|${r.acq_date}|${r.acq_time}|${r.latitude}|${r.longitude}`;
    if (seen.has(key)) continue;
    seen.add(key);
    out.push(r);
  }
  return out;
}

function csvUrl(dataset: string, bbox: string, days: number, endDate?: string): string {
  const base = `${env.FIRMS_BASE_URL}/${env.FIRMS_MAP_KEY}/${dataset}/${bbox}/${days}`;
  return endDate ? `${base}/${endDate}` : base;
}

function isUpstreamError(csv: string): boolean {
  // "Invalid..." error text or empty payload with no header row
  return !csv.includes("latitude");
}

async function fetchViaQueue(url: string, timeoutMs: number): Promise<string> {
  const { text } = await httpRequest(url, {}, { timeoutMs });
  return text;
}

/**
 * Fetch detections for `bbox` over the last `days` (1..5) from ALL active
 * sensor sources. These are the LIVE rows — used by the refresh job and
 * served to the client. Per-source failures degrade gracefully.
 */
export async function fetchRecent(bbox: string, days: number): Promise<FirmsCsvRow[]> {
  const d = Math.min(Math.max(1, Math.round(days)), FIRMS_MAX_REQUEST_DAYS);
  const sensors = resolveActiveSensorKeys();
  return FIRMS_QUEUE(async () => {
    const batches = await Promise.all(
      sensors.map(async (key) => {
        const spec = FIRMS_SENSOR_REGISTRY[key];
        try {
          const text = await fetchViaQueue(csvUrl(spec.source, bbox, d), 30_000);
          if (isUpstreamError(text)) {
            firmsLog.warn({ source: spec.source, bbox, days: d }, "FIRMS returned an error payload");
            return [];
          }
          const rows = parseCsv(text, spec.source);
          firmsLog.debug({ source: spec.source, bbox, days: d, rows: rows.length }, "FIRMS source fetch ok");
          return rows;
        } catch (err) {
          firmsLog.warn({ source: spec.source, bbox, err: String(err) }, "FIRMS source fetch failed — continuing");
          return [];
        }
      }),
    );
    const rows = dedupeRows(batches.flat());
    firmsLog.info(
      { bbox, days: d, rows: rows.length, sources: sensors.map((k) => FIRMS_SENSOR_REGISTRY[k].source) },
      "FIRMS recent fetch ok (multi-sensor)",
    );
    return rows;
  });
}

/**
 * Fetch detections for `bbox` over a window ENDING at `endDate`
 * (YYYY-MM-DD, UTC), `days` long (1..5). Archive/backfill shape.
 */
export async function fetchDated(
  bbox: string,
  days: number,
  endDate: string,
): Promise<FirmsCsvRow[]> {
  const d = Math.min(Math.max(1, Math.round(days)), FIRMS_MAX_REQUEST_DAYS);
  const sensors = resolveActiveSensorKeys();
  return FIRMS_QUEUE(async () => {
    const batches = await Promise.all(
      sensors.map(async (key) => {
        const spec = FIRMS_SENSOR_REGISTRY[key];
        try {
          const text = await fetchViaQueue(csvUrl(spec.source, bbox, d, endDate), 40_000);
          if (isUpstreamError(text)) return [];
          return parseCsv(text, spec.source);
        } catch (err) {
          firmsLog.warn({ source: spec.source, endDate, err: String(err) }, "FIRMS source fetch failed — continuing");
          return [];
        }
      }),
    );
    return dedupeRows(batches.flat());
  });
}
