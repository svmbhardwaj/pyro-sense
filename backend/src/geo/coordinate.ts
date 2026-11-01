/**
 * geo/coordinate — the ONE place coordinate order is decided (plan §8).
 *
 * Two conventions live in this codebase and they are easy to confuse:
 *
 *   - H3 (h3-js v4): [lat, lng] pairs everywhere. polygonToCells only wants
 *     [lng, lat] when explicitly called with isGeoJson: true.
 *   - GeoJSON / PostGIS: [lng, lat] (ST_MakePoint(lng, lat), GeoJSON spec).
 *
 * Every conversion between them goes through these helpers — never a bare
 * array literal with a silent opinion about order. Validity bounds catch
 * transposed input early (lat ±90, lng ±180), because a swapped pair at
 * Delhi reads as a valid coordinate 60° away — exactly the failure the plan
 * calls out.
 */

import type { BBox, LngLatTuple } from "./types.js";

export function isValidLat(lat: number): boolean {
  return Number.isFinite(lat) && lat >= -90 && lat <= 90;
}

export function isValidLng(lng: number): boolean {
  return Number.isFinite(lng) && lng >= -180 && lng <= 180;
}

/** H3 order → GeoJSON order. */
export function latLngToLngLat(lat: number, lng: number): LngLatTuple {
  if (!isValidLat(lat) || !isValidLng(lng)) {
    throw new RangeError(`invalid coordinates lat=${lat} lng=${lng}`);
  }
  return [lng, lat];
}

/** GeoJSON order → H3 order (returns the pair as separate values). */
export function lngLatToLatLng([lng, lat]: LngLatTuple): { lat: number; lng: number } {
  if (!isValidLat(lat) || !isValidLng(lng)) {
    throw new RangeError(`invalid coordinates lat=${lat} lng=${lng}`);
  }
  return { lat, lng };
}

/**
 * Build an H3-ready closed polygon from a bbox: [lat, lng] pairs, ring
 * closed. h3.polygonToCells consumes this directly (isGeoJson NOT set).
 */
export function bboxToH3Polygon(bbox: BBox): number[][] {
  return [
    [bbox.south, bbox.west],
    [bbox.south, bbox.east],
    [bbox.north, bbox.east],
    [bbox.north, bbox.west],
    [bbox.south, bbox.west], // closed ring
  ];
}

/**
 * Build a GeoJSON-ready closed polygon from a bbox: [lng, lat] pairs, ring
 * closed. Matches the GeoJSON spec (RFC 7946) for PostGIS ST_GeomFromGeoJSON
 * and any map library that speaks GeoJSON.
 */
export function bboxToGeoJsonPolygon(bbox: BBox): number[][] {
  return [
    [bbox.west, bbox.south],
    [bbox.east, bbox.south],
    [bbox.east, bbox.north],
    [bbox.west, bbox.north],
    [bbox.west, bbox.south], // closed ring
  ];
}

/** Parse "w,s,e,n" (the FIRMS/Overpass bbox string convention). */
export function parseBBoxString(s: string): BBox {
  const parts = s.split(",").map((p) => parseFloat(p.trim()));
  const [west, south, east, north] = parts;
  if (
    parts.length !== 4 ||
    west === undefined || south === undefined || east === undefined || north === undefined ||
    parts.some((n) => !Number.isFinite(n))
  ) {
    throw new RangeError(`invalid bbox string: ${s}`);
  }
  if (!isValidLat(south) || !isValidLat(north) || !isValidLng(west) || !isValidLng(east)) {
    throw new RangeError(`bbox out of range: ${s}`);
  }
  if (south >= north || west >= east) {
    throw new RangeError(`bbox edges inverted: ${s}`);
  }
  return { west, south, east, north };
}

/**
 * Normalize an antimeridian-crossing bbox into two non-crossing boxes.
 * A viewport like lng 175 → -175 (crossing the dateline) cannot be served
 * by a plain min/max box — it would span the whole world. Returns one box
 * when no crossing is present.
 */
export function splitAntimeridianBBox(bbox: BBox): BBox[] {
  if (bbox.west <= bbox.east) return [bbox];
  return [
    { west: bbox.west, south: bbox.south, east: 180, north: bbox.north },
    { west: -180, south: bbox.south, east: bbox.east, north: bbox.north },
  ];
}
