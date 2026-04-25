#!/usr/bin/env python3
"""Fetch OSM data via Overpass API and convert to GeoJSON.

Reads an Overpass QL query from query.overpassql, executes it against
multiple Overpass API endpoints with fallback, converts the response to
GeoJSON with full geometry support, and writes data.geojson.

Zero external dependencies — stdlib only.
"""

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

OVERPASS_ENDPOINTS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
]

QUERY_FILE = "query.overpassql"
OUTPUT_FILE = "data.geojson"
DEFAULT_DROP_THRESHOLD = 50  # percent
DEFAULT_MAX_DATA_LAG_HOURS = 48
REQUEST_TIMEOUT = 180  # seconds

# Tags that indicate a closed way should be treated as a Polygon (area)
# rather than a LineString. Based on XofY's isArea() and standard OSM conventions.
AREA_TAG_KEYS = {
    "building", "landuse", "natural", "leisure", "amenity", "shop",
    "boundary", "historic", "place", "area:highway", "craft", "office",
    "tourism", "aeroway",
}

# Specific tag=value pairs that indicate area semantics
AREA_TAG_VALUES = {
    ("highway", "rest_area"),
    ("highway", "services"),
    ("leisure", "track"),
    ("natural", "water"),
    ("waterway", "riverbank"),
    ("waterway", "dock"),
    ("waterway", "boatyard"),
}


# ---------------------------------------------------------------------------
# Query reading
# ---------------------------------------------------------------------------

def read_query(path):
    """Read the Overpass QL query from file."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            query = f.read().strip()
    except FileNotFoundError:
        print(f"Error: Query file '{path}' not found.", file=sys.stderr)
        sys.exit(1)

    if not query:
        print(f"Error: Query file '{path}' is empty.", file=sys.stderr)
        sys.exit(1)

    return query


# ---------------------------------------------------------------------------
# Overpass API fetching with multi-server fallback
# ---------------------------------------------------------------------------

def check_data_freshness(data, endpoint, max_lag_hours):
    """Check osm3s.timestamp_osm_base for data staleness.

    Returns (is_fresh, lag_hours, timestamp_str).
    """
    timestamp_str = data.get("osm3s", {}).get("timestamp_osm_base", "")
    if not timestamp_str:
        return True, 0, "(unknown)"

    try:
        data_time = datetime.fromisoformat(
            timestamp_str.replace("Z", "+00:00"))
        now = datetime.now(timezone.utc)
        lag_hours = (now - data_time).total_seconds() / 3600
        return lag_hours <= max_lag_hours, lag_hours, timestamp_str
    except (ValueError, TypeError):
        return True, 0, timestamp_str


def fetch_overpass(query):
    """Send query to Overpass API endpoints with fallback.

    Tries each endpoint in order. Handles rate limiting (HTTP 429),
    network errors, and data freshness validation.

    Returns parsed JSON response dict.
    """
    max_lag_hours = float(
        os.environ.get("TAP_IN_OSM_MAX_DATA_LAG_HOURS",
                       DEFAULT_MAX_DATA_LAG_HOURS)
    )
    encoded = urllib.parse.urlencode({"data": query}).encode("utf-8")
    last_error = None

    for endpoint in OVERPASS_ENDPOINTS:
        print(f"Trying {endpoint} ...")
        try:
            req = urllib.request.Request(
                endpoint,
                data=encoded,
                headers={"User-Agent": "tap-in-osm/1.0"},
            )
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
                body = resp.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            print(f"  HTTP {e.code}: {e.reason}", file=sys.stderr)
            last_error = e
            if e.code == 429:
                print("  Rate limited, waiting 5s before next server...",
                      file=sys.stderr)
                time.sleep(5)
            continue
        except urllib.error.URLError as e:
            print(f"  Network error: {e.reason}", file=sys.stderr)
            last_error = e
            continue
        except Exception as e:
            print(f"  Unexpected error: {e}", file=sys.stderr)
            last_error = e
            continue

        # Parse JSON
        try:
            data = json.loads(body)
        except json.JSONDecodeError as e:
            print(f"  Invalid JSON response: {e}", file=sys.stderr)
            print("  (Does your query include [out:json]?)", file=sys.stderr)
            last_error = e
            continue

        # Check for Overpass remark (error/warning in a 200 response)
        remark = data.get("remark")
        if remark and not data.get("elements"):
            print(f"  Overpass remark: {remark}", file=sys.stderr)
            last_error = Exception(remark)
            continue

        # Check data freshness
        is_fresh, lag_hours, ts = check_data_freshness(
            data, endpoint, max_lag_hours)
        if not is_fresh:
            print(
                f"  Data is {lag_hours:.1f}h old (timestamp: {ts}), "
                f"exceeds {max_lag_hours}h threshold. Trying next server...",
                file=sys.stderr,
            )
            last_error = Exception(f"Stale data from {endpoint}")
            continue

        if remark:
            print(f"  Overpass remark (non-fatal): {remark}")

        element_count = len(data.get("elements", []))
        print(f"  Success: {element_count} elements, data timestamp: {ts}")
        return data

    print("Error: All Overpass endpoints failed.", file=sys.stderr)
    if last_error:
        print(f"  Last error: {last_error}", file=sys.stderr)
    sys.exit(1)


# ---------------------------------------------------------------------------
# Geometry conversion helpers
# ---------------------------------------------------------------------------

def coords_from_geometry(geom_array):
    """Convert Overpass geometry array [{lat,lon},...] to GeoJSON [[lon,lat],...]."""
    coords = []
    for pt in geom_array:
        if pt is None:
            continue
        coords.append([pt["lon"], pt["lat"]])
    return coords


def is_area(tags):
    """Determine if a closed way should be treated as an area (Polygon).

    Based on XofY's isArea() logic and standard OSM area conventions.
    """
    if not tags:
        return False

    # Explicit area tag overrides everything
    if tags.get("area") == "yes":
        return True
    if tags.get("area") == "no":
        return False

    # Check for area-indicating tag keys
    for key in AREA_TAG_KEYS:
        if key in tags:
            return True

    # Check specific tag=value pairs
    for key, value in AREA_TAG_VALUES:
        if tags.get(key) == value:
            return True

    return False


def is_closed(coords):
    """Check if a coordinate ring is closed (first == last)."""
    if len(coords) < 4:
        return False
    return coords[0][0] == coords[-1][0] and coords[0][1] == coords[-1][1]


def point_in_polygon(point, ring):
    """Ray-casting point-in-polygon test.

    Returns True if point [lon, lat] is inside the ring [[lon,lat],...].
    """
    x, y = point
    inside = False
    n = len(ring)
    j = n - 1
    for i in range(n):
        xi, yi = ring[i]
        xj, yj = ring[j]
        if ((yi > y) != (yj > y)) and (x < (xj - xi) * (y - yi) / (yj - yi) + xi):
            inside = not inside
        j = i
    return inside


def ring_centroid(ring):
    """Compute a simple average centroid of a coordinate ring."""
    if not ring:
        return [0, 0]
    lons = [c[0] for c in ring]
    lats = [c[1] for c in ring]
    return [sum(lons) / len(lons), sum(lats) / len(lats)]


def merge_ways_into_rings(way_geometries):
    """Merge a list of way coordinate arrays into closed rings.

    way_geometries: list of [[lon,lat],...] coordinate arrays.

    Ways are stitched together by matching endpoints. Returns a list of
    closed rings (each a [[lon,lat],...] array where first == last).
    Any ways that cannot be merged into closed rings are returned as
    unclosed linestrings in a second list.
    """
    if not way_geometries:
        return [], []

    # Work with copies so we don't mutate input
    remaining = [list(wg) for wg in way_geometries if len(wg) >= 2]
    rings = []
    unclosed = []

    while remaining:
        # Start a new chain with the first remaining way
        current = remaining.pop(0)

        changed = True
        while changed:
            changed = False
            for i, candidate in enumerate(remaining):
                c_start = candidate[0]
                c_end = candidate[-1]
                cur_start = current[0]
                cur_end = current[-1]

                # Try to attach candidate to end of current
                if cur_end[0] == c_start[0] and cur_end[1] == c_start[1]:
                    current = current + candidate[1:]
                    remaining.pop(i)
                    changed = True
                    break
                elif cur_end[0] == c_end[0] and cur_end[1] == c_end[1]:
                    current = current + list(reversed(candidate))[1:]
                    remaining.pop(i)
                    changed = True
                    break
                # Try to attach candidate to start of current
                elif cur_start[0] == c_end[0] and cur_start[1] == c_end[1]:
                    current = candidate + current[1:]
                    remaining.pop(i)
                    changed = True
                    break
                elif cur_start[0] == c_start[0] and cur_start[1] == c_start[1]:
                    current = list(reversed(candidate)) + current[1:]
                    remaining.pop(i)
                    changed = True
                    break

        # Check if ring closed
        if is_closed(current):
            rings.append(current)
        else:
            unclosed.append(current)

    return rings, unclosed


def build_multipolygon(members):
    """Build a GeoJSON MultiPolygon/Polygon geometry from relation members.

    members: list of Overpass relation members with geometry.

    Returns a GeoJSON geometry dict, or None if no geometry can be built.
    """
    outer_ways = []
    inner_ways = []

    for member in members:
        if member.get("type") != "way":
            continue
        geom = member.get("geometry")
        if not geom:
            continue
        coords = coords_from_geometry(geom)
        if len(coords) < 2:
            continue

        role = member.get("role", "outer")
        if role == "inner":
            inner_ways.append(coords)
        else:
            outer_ways.append(coords)

    # Merge ways into closed rings
    outer_rings, outer_unclosed = merge_ways_into_rings(outer_ways)
    inner_rings, _ = merge_ways_into_rings(inner_ways)

    if not outer_rings:
        # Can't form any closed outer rings — fall back to unclosed geometry
        if outer_unclosed:
            if len(outer_unclosed) == 1:
                return {"type": "LineString", "coordinates": outer_unclosed[0]}
            return {"type": "MultiLineString", "coordinates": outer_unclosed}
        return None

    # Assign inner rings to their containing outer ring
    # Each polygon = [outer_ring, inner1, inner2, ...]
    polygons = [[ring] for ring in outer_rings]

    for inner in inner_rings:
        centroid = ring_centroid(inner)
        assigned = False
        for polygon in polygons:
            outer = polygon[0]
            if point_in_polygon(centroid, outer):
                polygon.append(inner)
                assigned = True
                break
        if not assigned and polygons:
            # Default to first outer ring if containment test fails
            polygons[0].append(inner)

    if len(polygons) == 1:
        return {"type": "Polygon", "coordinates": polygons[0]}
    return {"type": "MultiPolygon", "coordinates": polygons}


# ---------------------------------------------------------------------------
# Element to GeoJSON Feature conversion
# ---------------------------------------------------------------------------

def element_to_feature(element):
    """Convert a single Overpass JSON element to a GeoJSON Feature.

    Returns a Feature dict, or None if the element has no usable geometry.
    """
    elem_type = element.get("type", "")
    elem_id = element.get("id", 0)
    tags = element.get("tags", {})

    properties = dict(tags)
    properties["@id"] = f"{elem_type}/{elem_id}"
    properties["@type"] = elem_type

    geometry = None

    if elem_type == "node":
        if "lat" in element and "lon" in element:
            geometry = {
                "type": "Point",
                "coordinates": [element["lon"], element["lat"]],
            }

    elif elem_type == "way":
        geom_array = element.get("geometry")
        if geom_array:
            coords = coords_from_geometry(geom_array)
            if len(coords) >= 2:
                if is_closed(coords) and is_area(tags):
                    geometry = {"type": "Polygon", "coordinates": [coords]}
                else:
                    geometry = {"type": "LineString", "coordinates": coords}

        # Fallback to center point if no geometry array
        if geometry is None:
            center = element.get("center")
            if center:
                geometry = {
                    "type": "Point",
                    "coordinates": [center["lon"], center["lat"]],
                }

    elif elem_type == "relation":
        rel_type = tags.get("type", "")
        members = element.get("members", [])

        if rel_type in ("multipolygon", "boundary") and members:
            geometry = build_multipolygon(members)

        elif rel_type == "route" and members:
            lines = []
            for member in members:
                if member.get("type") == "way" and member.get("geometry"):
                    coords = coords_from_geometry(member["geometry"])
                    if len(coords) >= 2:
                        lines.append(coords)
                elif member.get("type") == "node":
                    # Skip node members of routes (stops, etc.)
                    pass
            if lines:
                if len(lines) == 1:
                    geometry = {"type": "LineString", "coordinates": lines[0]}
                else:
                    geometry = {"type": "MultiLineString",
                                "coordinates": lines}

        # Fallback for relations without member geometry
        if geometry is None:
            center = element.get("center")
            if center:
                geometry = {
                    "type": "Point",
                    "coordinates": [center["lon"], center["lat"]],
                }
            elif element.get("bounds"):
                b = element["bounds"]
                geometry = {
                    "type": "Point",
                    "coordinates": [
                        (b["minlon"] + b["maxlon"]) / 2,
                        (b["minlat"] + b["maxlat"]) / 2,
                    ],
                }

    if geometry is None:
        return None

    return {
        "type": "Feature",
        "geometry": geometry,
        "properties": properties,
    }


def elements_to_features(elements):
    """Convert all Overpass elements to GeoJSON Features."""
    features = []
    skipped = 0
    for element in elements:
        feature = element_to_feature(element)
        if feature:
            features.append(feature)
        else:
            skipped += 1

    if skipped:
        print(
            f"  Skipped {skipped} elements without usable geometry.",
            file=sys.stderr,
        )

    return features


# ---------------------------------------------------------------------------
# Safety checks
# ---------------------------------------------------------------------------

def check_feature_drop(new_count, output_path, threshold):
    """Compare new feature count against existing file.

    Exits with error if the drop exceeds the threshold percentage.
    """
    if not os.path.exists(output_path):
        print("No existing data file found, skipping reduction check.")
        return

    try:
        with open(output_path, "r", encoding="utf-8") as f:
            existing = json.load(f)
        old_count = len(existing.get("features", []))
    except (json.JSONDecodeError, KeyError, TypeError) as e:
        print(
            f"  Warning: Could not parse existing {output_path}: {e}", file=sys.stderr)
        print("  Skipping reduction check.", file=sys.stderr)
        return

    if old_count == 0:
        print(f"Existing file has 0 features, skipping reduction check.")
        return

    drop_pct = ((old_count - new_count) / old_count) * 100

    if drop_pct > threshold:
        print(
            f"SAFETY CHECK FAILED: Feature count dropped from {old_count} to "
            f"{new_count} ({drop_pct:.1f}% reduction, threshold is {threshold}%).\n"
            f"This may indicate a partial Overpass response. Not updating the file.",
            file=sys.stderr,
        )
        sys.exit(1)

    change_pct = -drop_pct  # positive = increase
    print(
        f"Safety check passed: {old_count} -> {new_count} features ({change_pct:+.1f}%).")


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def sort_key(feature):
    """Sort key for canonical feature ordering: node < way < relation, then by ID."""
    type_order = {"node": 0, "way": 1, "relation": 2}
    props = feature.get("properties", {})
    osm_type = props.get("@type", "")
    # Extract numeric ID from "@id" like "node/12345"
    osm_id_str = props.get("@id", "")
    try:
        osm_id = int(osm_id_str.split("/", 1)[1])
    except (IndexError, ValueError):
        osm_id = 0
    return (type_order.get(osm_type, 9), osm_id)


def write_geojson(features, path):
    """Write a GeoJSON FeatureCollection to file, sorted by OSM ID."""
    features = sorted(features, key=sort_key)
    geojson = {
        "type": "FeatureCollection",
        "features": features,
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(geojson, f, ensure_ascii=False, indent=2)
        f.write("\n")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    query = read_query(QUERY_FILE)
    data = fetch_overpass(query)

    elements = data.get("elements", [])
    features = elements_to_features(elements)

    if not features:
        print("Error: Query returned zero usable features.", file=sys.stderr)
        sys.exit(1)

    print(f"Converted {len(features)} features to GeoJSON.")

    threshold = int(os.environ.get(
        "TAP_IN_OSM_DROP_THRESHOLD", DEFAULT_DROP_THRESHOLD))
    check_feature_drop(len(features), OUTPUT_FILE, threshold)

    write_geojson(features, OUTPUT_FILE)
    print(f"Wrote {len(features)} features to {OUTPUT_FILE}.")


if __name__ == "__main__":
    main()
