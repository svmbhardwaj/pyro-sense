/**
 * geo/h3 — the ONE wrapper around h3-js (plan §8). Every H3 polygon/cell
 * conversion in the backend goes through here so coordinate order and ring
 * closure are decided exactly once, not per call site.
 */

import h3 from "h3-js";

import { bboxToH3Polygon, splitAntimeridianBBox } from "./coordinate.js";
import type { BBox } from "./types.js";

/** All res-7 cells covering a viewport bbox (antimeridian handled). */
export function cellsForBBox(bbox: BBox, res = 7, maxCells = 5000): string[] {
  const set = new Set<string>();
  for (const box of splitAntimeridianBBox(bbox)) {
    // bboxToH3Polygon emits [lat, lng] closed rings — h3's native order.
    for (const cell of h3.polygonToCells(bboxToH3Polygon(box), res)) {
      set.add(cell);
    }
  }
  return [...set].slice(0, maxCells);
}

/** Closed bounding box of an H3 cell in named BBox fields (plan §8). */
export function cellBBox(h3Cell: string): BBox {
  // h3-js v4 cellToBoundary returns [lat, lng] pairs (formatAsGeoJson=false).
  const boundary = h3.cellToBoundary(h3Cell);
  const lats = boundary.map((p) => p[0]);
  const lngs = boundary.map((p) => p[1]);
  return {
    west: Math.min(...lngs),
    south: Math.min(...lats),
    east: Math.max(...lngs),
    north: Math.max(...lats),
  };
}
