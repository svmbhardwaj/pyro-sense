/**
 * geo/distance — great-circle distance on the sphere (plan §8: one distance
 * implementation, tested). Good to ~0.3% vs the geodesic; PostGIS geography
 * stays the authority for exact queries (§12) — this is for in-process
 * checks (radius filters, matching scores) where a query would be wasteful.
 */

const EARTH_RADIUS_KM = 6371.0088;

export function haversineKm(
  lat1: number,
  lng1: number,
  lat2: number,
  lng2: number,
): number {
  const φ1 = (lat1 * Math.PI) / 180;
  const φ2 = (lat2 * Math.PI) / 180;
  const Δφ = ((lat2 - lat1) * Math.PI) / 180;
  const Δλ = ((lng2 - lng1) * Math.PI) / 180;

  const a =
    Math.sin(Δφ / 2) ** 2 + Math.cos(φ1) * Math.cos(φ2) * Math.sin(Δλ / 2) ** 2;
  return 2 * EARTH_RADIUS_KM * Math.asin(Math.sqrt(a));
}
