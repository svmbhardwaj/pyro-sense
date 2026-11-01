/**
 * firmsClient tests — node:test runner (Node ≥20, tsx import loader).
 *
 * Phase B (plan §9): multi-sensor ingestion. Pure-function coverage:
 *  1. resolveActiveSensorKeys — NOAA-20 + NOAA-21 primary; S-NPP drops out
 *     after its 2026-11-01 end-of-life; explicit FIRMS_SENSORS override.
 *  2. parseCsv — provenance tagging (source dataset id per row), malformed
 *     rows skipped, error payloads → [].
 *  3. dedupeRows — natural-key dedupe across merged multi-source batches.
 *
 * run: npm test  (from backend/)
 */

import assert from "node:assert/strict";
import test from "node:test";

import {
  ALL_SENSOR_KEYS,
  FIRMS_SENSOR_REGISTRY,
  SNPP_END_OF_LIFE,
  dedupeRows,
  parseCsv,
  resolveActiveSensorKeys,
  type FirmsCsvRow,
} from "./firmsClient.js";

const VIIRS_CSV = `latitude,longitude,bright_ti4,bright_ti5,scan,track,frp,acq_date,acq_time,satellite,instrument,confidence,version,bright_t31,daynight
28.61,77.20,344.1,301.2,0.42,0.40,7.5,2026-10-05,1345,N20,VIIRS,90,2.0NRT,300.1,D
28.72,77.10,340.0,299.0,0.5,0.4,5.0,2026-10-05,1345,N20,VIIRS,85,2.0NRT,298.0,N
bad,rows,here
28.90,77.30,350.0,305.0,0.6,0.5,9.0,2026-10-05,1355,N21,VIIRS,l,2.0NRT,301.0,D`;

function row(overrides: Partial<FirmsCsvRow>): FirmsCsvRow {
  return {
    latitude: 28.61,
    longitude: 77.2,
    bright_ti4: 344.1,
    bright_ti5: 301.2,
    scan: 0.42,
    track: 0.4,
    frp: 7.5,
    acq_date: "2026-10-05",
    acq_time: "1345",
    satellite: "N20",
    instrument: "VIIRS",
    confidence: "90",
    daynight: "D",
    source: "VIIRS_NOAA20_NRT",
    ...overrides,
  };
}

// ── sensor registry / active-source resolution ───────────────────────────

test("registry has NOAA-20, NOAA-21 primary and S-NPP transitional", () => {
  assert.deepEqual(ALL_SENSOR_KEYS.sort(), ["NOAA20", "NOAA21", "SNPP"]);
  assert.equal(FIRMS_SENSOR_REGISTRY.NOAA20.lifecycle, "primary");
  assert.equal(FIRMS_SENSOR_REGISTRY.NOAA21.lifecycle, "primary");
  assert.equal(FIRMS_SENSOR_REGISTRY.SNPP.lifecycle, "transitional");
  assert.equal(FIRMS_SENSOR_REGISTRY.SNPP.endOfLife, "2026-11-01");
});

test("S-NPP is active before its end-of-life and drops out on it", () => {
  const before = resolveActiveSensorKeys(new Date("2026-10-31T23:59:59Z"));
  const at = resolveActiveSensorKeys(new Date("2026-11-01T00:00:01Z"));
  const after = resolveActiveSensorKeys(new Date("2027-01-01T00:00:00Z"));
  assert.ok(before.includes("SNPP"), "S-NPP should be active in October 2026");
  assert.ok(!at.includes("SNPP"), "S-NPP must drop out ON 2026-11-01");
  assert.ok(!after.includes("SNPP"));
  assert.deepEqual(at.sort(), ["NOAA20", "NOAA21"]);
  assert.deepEqual(after.sort(), ["NOAA20", "NOAA21"]);
});

test("FIRMS_SENSORS override pins an explicit subset", () => {
  // Even before S-NPP EOL, an explicit pin wins.
  const keys = resolveActiveSensorKeys(new Date("2026-10-01T00:00:00Z"), "NOAA20,NOAA21");
  assert.deepEqual(keys.sort(), ["NOAA20", "NOAA21"]);
  // No override (undefined, not empty): defaults apply — S-NPP pre-EOL active.
  const defaults = resolveActiveSensorKeys(new Date("2026-10-01T00:00:00Z"), undefined);
  assert.ok(defaults.includes("SNPP"));
});

test("unknown FIRMS_SENSORS keys are dropped, valid ones kept", () => {
  const keys = resolveActiveSensorKeys(new Date("2026-10-01T00:00:00Z"), "NOAA21,SENTINEL-9");
  assert.deepEqual(keys, ["NOAA21"]);
});

// ── CSV parsing with provenance ──────────────────────────────────────────

test("parseCsv tags every row with the FIRMS source dataset id", () => {
  const rows = parseCsv(VIIRS_CSV, "VIIRS_NOAA20_NRT");
  assert.equal(rows.length, 3);
  assert.ok(rows.every((r) => r.source === "VIIRS_NOAA20_NRT"));
  assert.equal(rows[0]!.satellite, "N20");
  assert.equal(rows[2]!.confidence, "l"); // low-confidence string preserved as-is
});

test("parseCsv skips malformed rows and non-finite coordinates", () => {
  const rows = parseCsv("latitude,longitude,frp\n28.5,77.2,1.0\nnotanumber,77.2,1.0\n");
  assert.equal(rows.length, 1);
});

test("parseCsv returns [] on upstream error payloads", () => {
  assert.deepEqual(parseCsv("Invalid day range. Expects [1..5]."), []);
  assert.deepEqual(parseCsv(""), []);
});

// ── in-batch dedupe (plan §48) ───────────────────────────────────────────

test("dedupeRows removes exact natural-key duplicates within a merged batch", () => {
  const a = row({});
  const aDup = row({}); // same source+satellite+time+position
  const b = row({ source: "VIIRS_NOAA21_NRT", satellite: "N21" }); // different sensor — kept
  const c = row({ acq_time: "1355" }); // different pass — kept
  const merged = dedupeRows([a, aDup, b, c]);
  assert.equal(merged.length, 3);
  assert.ok(merged.includes(b) && merged.includes(c));
});

test("dedupeRows keeps rows differing only in rounded coordinate noise", () => {
  const a = row({});
  const shifted = row({ latitude: 28.6101 });
  assert.equal(dedupeRows([a, shifted]).length, 2);
});

test("SNPP_END_OF_LIFE matches the NASA transition date", () => {
  assert.equal(SNPP_END_OF_LIFE, "2026-11-01");
});
