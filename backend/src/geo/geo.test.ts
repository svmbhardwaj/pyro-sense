/**
 * geo/ tests — node:test runner. Locks the coordinate-order contract (plan §8):
 * H3 wants [lat,lng] (unless isGeoJson), GeoJSON/PostGIS want [lng,lat].
 * Every geometry the plan demands is covered: Delhi viewport, New York
 * viewport, cross-dateline viewport, small viewport.
 *
 * run: npm test  (from backend/)
 */

import assert from "node:assert/strict";
import test from "node:test";

import {
  bboxToGeoJsonPolygon,
  bboxToH3Polygon,
  latLngToLngLat,
  lngLatToLatLng,
  parseBBoxString,
  splitAntimeridianBBox,
} from "./coordinate.js";
import { haversineKm } from "./distance.js";
import { cellBBox, cellsForBBox } from "./h3.js";

const DELHI_BBOX = { west: 76.8, south: 28.3, east: 77.6, north: 28.9 };
const NY_BBOX = { west: -74.3, south: 40.4, east: -73.6, north: 41.0 };
const DATELINE_BBOX = { west: 175, south: -50, east: -175, north: -40 };
// A NARROW dateline strip (~22 km wide): wide enough for cells on both
// sides, narrow enough to stay far below the 5000-cell cap.
const DATELINE_STRIP = { west: 179.9, south: -41, east: -179.9, north: -40 };
// ~5.5 km × 5.5 km — spans a few res-7 cells (avg cell edge ≈ 1.2 km).
const SMALL_BBOX = { west: 77.20, south: 28.61, east: 77.25, north: 28.65 };

test("latLngToLngLat converts H3 order to GeoJSON order", () => {
  assert.deepEqual(latLngToLngLat(28.61, 77.21), [77.21, 28.61]);
});

test("lngLatToLatLng converts GeoJSON order to H3 order", () => {
  assert.deepEqual(lngLatToLatLng([77.21, 28.61]), { lat: 28.61, lng: 77.21 });
});

test("transposed coordinates are caught when lat position exceeds ±90", () => {
  // Tokyo swapped: lat 139.69 is impossible — the swap must throw.
  assert.throws(() => latLngToLngLat(139.69, 35.68), RangeError);
  assert.throws(() => lngLatToLatLng([35.68, 139.69]), RangeError);
  // NOTE: a swapped pair whose "lat" is still ≤90 (e.g. Delhi) is a valid
  // coordinate far away — range validation is a backstop, not a proof. The
  // real defense is that every conversion goes through THIS module (plan §8).
});

test("bboxToH3Polygon is [lat,lng] and closed", () => {
  const ring = bboxToH3Polygon(DELHI_BBOX);
  assert.equal(ring.length, 5);
  assert.deepEqual(ring[0], ring[4]);
  assert.equal(ring[0]![0], DELHI_BBOX.south); // lat first
  assert.equal(ring[0]![1], DELHI_BBOX.west); // lng second
});

test("bboxToGeoJsonPolygon is [lng,lat] and closed", () => {
  const ring = bboxToGeoJsonPolygon(DELHI_BBOX);
  assert.equal(ring.length, 5);
  assert.deepEqual(ring[0], ring[4]);
  assert.equal(ring[0]![0], DELHI_BBOX.west); // lng first
  assert.equal(ring[0]![1], DELHI_BBOX.south); // lat second
});

test("parseBBoxString reads w,s,e,n and rejects junk", () => {
  assert.deepEqual(parseBBoxString("68,6,98,36"), { west: 68, south: 6, east: 98, north: 36 });
  assert.throws(() => parseBBoxString("1,2,3"), RangeError);
  assert.throws(() => parseBBoxString("36,6,98,6"), RangeError); // south >= north
  assert.throws(() => parseBBoxString("a,b,c,d"), RangeError);
});

test("antimeridian bbox splits into two boxes", () => {
  const parts = splitAntimeridianBBox(DATELINE_BBOX);
  assert.equal(parts.length, 2);
  assert.deepEqual(parts[0], { west: 175, south: -50, east: 180, north: -40 });
  assert.deepEqual(parts[1], { west: -180, south: -50, east: -175, north: -40 });
});

test("normal bbox passes through antimeridian split unsplit", () => {
  assert.deepEqual(splitAntimeridianBBox(DELHI_BBOX), [DELHI_BBOX]);
});

// ── H3 cells: the four viewports the plan demands ────────────────────────

test("Delhi viewport: cells center on Delhi, not the transposed point", () => {
  const cells = cellsForBBox(DELHI_BBOX);
  assert.ok(cells.length > 0 && cells.length <= 5000);
  // Center cell of the viewport must resolve near Delhi, proving [lat,lng]
  // order — a transposed polygon would produce cells near (77.6E is fine but
  // 28.9 as lng puts the box in the Indian Ocean / wrong hemisphere).
  const center = cells[Math.floor(cells.length / 2)]!;
  const [lng, lat] = h3CellToLngLat(center);
  assert.ok(lat > 27.5 && lat < 29.5, `lat ${lat} should be near Delhi`);
  assert.ok(lng > 76 && lng < 78.5, `lng ${lng} should be near Delhi`);
});

test("New York viewport: negative lngs handled, cells near NYC", () => {
  const cells = cellsForBBox(NY_BBOX);
  assert.ok(cells.length > 0);
  const center = cells[Math.floor(cells.length / 2)]!;
  const [lng, lat] = h3CellToLngLat(center);
  assert.ok(lat > 39.5 && lat < 41.5);
  assert.ok(lng > -75.5 && lng < -72.5);
});

test("cross-dateline viewport: produces cells on BOTH sides without covering the world", () => {
  const cells = cellsForBBox(DATELINE_STRIP);
  assert.ok(cells.length > 0 && cells.length < 3000, `expected a bounded set, got ${cells.length}`);
  let eastSide = 0;
  let westSide = 0;
  for (const cell of cells) {
    const [lng] = h3CellToLngLat(cell);
    if (lng >= 170) eastSide++;
    else if (lng <= -170) westSide++;
  }
  assert.ok(eastSide > 0, "should have cells east of the dateline");
  assert.ok(westSide > 0, "should have cells west of the dateline");
});

test("wide dateline viewport: capped, never a world-spanning polygon", () => {
  // The 10°-wide version would be ~166k cells; the cap must hold.
  const cells = cellsForBBox(DATELINE_BBOX, 7, 5000);
  assert.equal(cells.length, 5000);
});

test("small viewport: a handful of res-7 cells", () => {
  const cells = cellsForBBox(SMALL_BBOX);
  assert.ok(cells.length >= 1 && cells.length <= 25, `got ${cells.length}`);
});

test("sub-cell viewport: 0 cells is valid center-containment behavior", () => {
  // h3 v4 polygonToCells defaults to center containment: a box smaller than
  // one cell can contain no cell CENTER. Callers must tolerate [] — the
  // risk route already treats that as an empty (cached) response.
  const tiny = { west: 77.200, south: 28.610, east: 77.203, north: 28.612 };
  assert.ok(Array.isArray(cellsForBBox(tiny)));
});

test("cellBBox returns a valid containing box for a known cell", () => {
  const cell = cellsForBBox(DELHI_BBOX)[0]!;
  const box = cellBBox(cell);
  const center = h3CellToLngLat(cell);
  assert.ok(center[1] >= box.south && center[1] <= box.north);
  assert.ok(center[0] >= box.west && center[0] <= box.east);
});

// ── distance ─────────────────────────────────────────────────────────────

test("haversineKm: Delhi↔NY ≈ 11,760 km", () => {
  const d = haversineKm(28.6139, 77.209, 40.7128, -74.006);
  assert.ok(d > 11_500 && d < 12_000, `got ${d}`);
});

test("haversineKm: same point is zero", () => {
  assert.equal(haversineKm(28.61, 77.2, 28.61, 77.2), 0);
});

test("haversineKm: 1° of latitude ≈ 111.2 km", () => {
  const d = haversineKm(28, 77, 29, 77);
  assert.ok(d > 110.5 && d < 112, `got ${d}`);
});

// ── helper ───────────────────────────────────────────────────────────────

import h3 from "h3-js";

function h3CellToLngLat(cell: string): [number, number] {
  const [lat, lng] = h3.cellToLatLng(cell);
  return [lng, lat];
}
