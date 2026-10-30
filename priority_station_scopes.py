"""Versioned, fixed station sets for the two collector entry points."""
from functools import lru_cache
import json
from pathlib import Path

SCOPE_MODES = {
    "MONITOR_BAIDU_MIN240_499": "BAIDU_MIN240_499",
    "MONITOR_AMAP_MIN240_647": "AMAP_MIN240_647",
}
EXPECTED_SCOPES = {
    "BAIDU_MIN240_499": ("BAIDU", "DETAIL", 499),
    "AMAP_MIN240_647": ("AMAP", "LIST", 647),
}


@lru_cache(maxsize=1)
def load_scopes():
    path = Path(__file__).resolve().parent / "config" / "priority_station_scopes.json"
    scopes = json.loads(path.read_text(encoding="utf-8"))["scopes"]
    all_pois = set()
    for key, (collector, mode, count) in EXPECTED_SCOPES.items():
        scope = scopes[key]
        stations = scope["stations"]
        pois = {station["source_station_id"] for station in stations}
        keys = {station["source_key"] for station in stations}
        if (scope["collector"] != collector or scope["collection_mode"] != mode
                or scope["count"] != count or len(stations) != count
                or len(pois) != count or len(keys) != count
                or "" in pois or "" in keys or all_pois.intersection(pois)
                or any(not station["station_name"].strip() for station in stations)):
            raise RuntimeError(f"INVALID_PRIORITY_STATION_SCOPE: {key}")
        all_pois.update(pois)
    return scopes


def scope_counts():
    return {key: scope["count"] for key, scope in load_scopes().items()}


def candidate_rows(conn, scope_key):
    scope = load_scopes()[scope_key]
    stations = scope["stations"]
    keys = [station["source_key"] for station in stations]
    with conn.cursor() as cursor:
        cursor.execute(
            "SELECT source_key,source_station_id,city,district,source_address,"
            "collected_address,source_latitude,source_longitude,result_payload "
            "FROM site_exploration_charging_station_result WHERE source_key IN ("
            + ",".join(["%s"] * len(keys)) + ")", keys,
        )
        rows = {row["source_key"]: row for row in cursor.fetchall()}
    if len(rows) != scope["count"]:
        raise RuntimeError(f"PRIORITY_STATION_RESULTS_MISSING: {scope_key}")
    candidates = []
    for station in stations:
        row = rows[station["source_key"]]
        if row["source_station_id"] != station["source_station_id"]:
            raise RuntimeError(f"PRIORITY_STATION_ID_MISMATCH: {scope_key}")
        payload = row.get("result_payload") or {}
        if isinstance(payload, str):
            payload = json.loads(payload)
        candidates.append({
            "source_key": station["source_key"],
            "source_station_id": station["source_station_id"],
            "matched_station_name": station["station_name"],
            "city": station.get("city") or row.get("city") or "",
            "district": station.get("district") or row.get("district") or "",
            "station_address": row.get("source_address") or row.get("collected_address") or payload.get("address") or "",
            "latitude": row.get("source_latitude"),
            "longitude": row.get("source_longitude"),
        })
    return candidates
