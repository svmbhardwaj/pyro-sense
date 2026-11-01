/** Shared geo types — [lng, lat] is ALWAYS GeoJSON order (plan §8). */

/** A single [lng, lat] pair, GeoJSON/PostGIS order. */
export type LngLatTuple = [number, number];

/** Bounding box in named components (never an ambiguous tuple). */
export interface BBox {
  west: number;
  south: number;
  east: number;
  north: number;
}
