"""Merge the retired Henan result table into the unified result table.

Defaults to a read-only plan. Pass --apply to migrate and drop only the retired
`henan_heavy_truck_charging_station` result table. The Henan task table is kept.
"""
from __future__ import annotations

import argparse
import gzip
import json
import os
import sys
from collections import defaultdict
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from urllib.parse import unquote, urlparse

import pymysql

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
BACKUP_DIR = ROOT / "backups"
TARGET = "site_exploration_charging_station_result"
LEGACY = "henan_heavy_truck_charging_station"
TASKS = "henan_heavy_truck_charging_station_task"


def load_env() -> None:
    path = ROOT / ".env"
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def db_config():
    load_env()
    parsed = urlparse(os.environ["EVCS_DATABASE_URL"].replace(r"\@", "@"))
    return {
        "host": parsed.hostname,
        "port": parsed.port or 3306,
        "user": unquote(parsed.username or ""),
        "password": unquote(parsed.password or ""),
        "database": parsed.path.lstrip("/"),
        "charset": "utf8mb4",
        "cursorclass": pymysql.cursors.DictCursor,
        "connect_timeout": 10,
    }


def json_default(value):
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (datetime,)):
        return value.isoformat()
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    raise TypeError(type(value).__name__)


def json_value(value, fallback):
    if value is None:
        return fallback
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return fallback


def row_time(row):
    for key in ("captured_at", "updated_at", "collected_at", "received_at"):
        value = row.get(key)
        if value not in (None, "", 0, "0"):
            try:
                return int(value)
            except (TypeError, ValueError):
                pass
    return 0


def main(apply: bool) -> None:
    from site_exploration_bridge import SiteExplorationBridge, station_source_key

    config = db_config()
    bridge_config = {key: value for key, value in config.items() if key != "cursorclass"}
    bridge = SiteExplorationBridge(bridge_config, enabled=True)
    if apply:
        bridge.ensure_result_table()
    conn = pymysql.connect(**config)
    try:
        with conn.cursor() as cursor:
            cursor.execute(f"SELECT * FROM `{TARGET}` ORDER BY id")
            target_rows = cursor.fetchall()
            cursor.execute(f"SELECT * FROM `{LEGACY}` ORDER BY id")
            legacy_rows = cursor.fetchall()
            cursor.execute(f"SELECT * FROM `{TASKS}` ORDER BY updated_at DESC, id DESC")
            task_rows = cursor.fetchall()

        groups = defaultdict(list)
        for row in target_rows:
            station_name = row.get("matched_station_name") or row.get("requested_name") or ""
            key = station_source_key(row.get("source_station_id"), station_name)
            groups[key].append(row)
        canonical_target_count = len(groups)

        task_by_name = {}
        for task in task_rows:
            name = str(task.get("station_name") or "").strip()
            if name and name not in task_by_name:
                task_by_name[name] = task

        ids_by_name = defaultdict(set)
        for row in target_rows:
            station_id = str(row.get("source_station_id") or "").strip()
            if not station_id:
                continue
            for name in (row.get("matched_station_name"), row.get("requested_name")):
                name = str(name or "").strip()
                if name:
                    ids_by_name[name].add(station_id)

        legacy_keys = set()
        mapped_by_task = 0
        mapped_by_existing_name = 0
        for row in legacy_rows:
            name = str(row.get("station_name") or "").strip()
            task = task_by_name.get(name)
            station_id = str(task.get("amap_poi_id") or "").strip() if task else ""
            if station_id:
                mapped_by_task += 1
            elif len(ids_by_name.get(name, set())) == 1:
                station_id = next(iter(ids_by_name[name]))
                mapped_by_existing_name += 1
            legacy_keys.add(station_source_key(station_id, name))

        expected_max = len(set(groups) | legacy_keys)
        print(json.dumps({
            "targetRowsBefore": len(target_rows),
            "targetCanonicalStations": canonical_target_count,
            "legacyRows": len(legacy_rows),
            "legacyMappedByTaskPoiId": mapped_by_task,
            "legacyMappedByExistingResultName": mapped_by_existing_name,
            "expectedUnifiedRows": expected_max,
            "apply": apply,
        }, ensure_ascii=False, indent=2))
        if not apply:
            return

        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        backup_path = BACKUP_DIR / f"unified-result-migration-{timestamp}.json.gz"
        with gzip.open(backup_path, "wt", encoding="utf-8") as handle:
            json.dump({"target": target_rows, "legacy": legacy_rows, "tasks": task_rows}, handle,
                      ensure_ascii=False, default=json_default)

        with conn.cursor() as cursor:
            # Canonicalize the existing nearby-station rows and retain only the newest
            # observation for each Amap POI id.
            for key, rows in groups.items():
                winner = max(rows, key=lambda item: (row_time(item), int(item["id"])))
                loser_ids = [int(item["id"]) for item in rows if item["id"] != winner["id"]]
                if loser_ids:
                    placeholders = ",".join("%s" for _ in loser_ids)
                    cursor.execute(f"DELETE FROM `{TARGET}` WHERE id IN ({placeholders})", loser_ids)
                payload = json_value(winner.get("result_payload"), {})
                cursor.execute(
                    f"""UPDATE `{TARGET}`
                        SET source_key=%s,
                            collection_source=COALESCE(NULLIF(collection_source,''),'SITE_EXPLORATION'),
                            province=COALESCE(NULLIF(province,''),%s),
                            city=COALESCE(NULLIF(city,''),%s),
                            district=COALESCE(NULLIF(district,''),%s)
                        WHERE id=%s""",
                    (
                        key,
                        str(payload.get("province") or ""),
                        str(payload.get("city") or ""),
                        str(payload.get("district") or ""),
                        winner["id"],
                    ),
                )
            conn.commit()

            cursor.execute(f"SELECT * FROM `{TARGET}`")
            current_rows = cursor.fetchall()
            current_by_key = {row["source_key"]: row for row in current_rows}

            inserted = 0
            overwritten = 0
            kept_newer = 0
            for legacy in sorted(legacy_rows, key=lambda item: (row_time(item), int(item["id"]))):
                name = str(legacy.get("station_name") or "").strip()
                task = task_by_name.get(name)
                station_id = str(task.get("amap_poi_id") or "").strip() if task else ""
                if not station_id and len(ids_by_name.get(name, set())) == 1:
                    station_id = next(iter(ids_by_name[name]))
                key = station_source_key(station_id, name)
                current = current_by_key.get(key)
                incoming_time = row_time(legacy)
                if current is not None and incoming_time < row_time(current):
                    kept_newer += 1
                    continue

                fast_prices = json_value(legacy.get("fast_prices"), [])
                slow_prices = json_value(legacy.get("slow_prices"), [])
                facilities = json_value(legacy.get("facilities"), [])
                tags = json_value(legacy.get("tags"), [])
                payload = {
                    "province": legacy.get("province") or "河南省",
                    "city": legacy.get("city") or (task.get("city") if task else ""),
                    "district": task.get("district") if task else "",
                    "stationName": name,
                    "operator": legacy.get("operator") or "",
                    "address": legacy.get("address") or "",
                    "businessHours": legacy.get("business_hours") or "",
                    "currentPrice": legacy.get("current_price") or "",
                    "parkingFee": legacy.get("parking_fee") or "",
                    "occupancyFee": legacy.get("occupancy_fee") or "",
                    "longitude": float(legacy.get("longitude") or 0),
                    "latitude": float(legacy.get("latitude") or 0),
                    "fastAvailable": legacy.get("fast_available") or "",
                    "fastTotal": legacy.get("fast_total") or "",
                    "fastPower": legacy.get("fast_power") or "",
                    "superAvailable": legacy.get("super_available") or "",
                    "superTotal": legacy.get("super_total") or "",
                    "superPower": legacy.get("super_power") or "",
                    "slowAvailable": legacy.get("slow_available") or "",
                    "slowTotal": legacy.get("slow_total") or "",
                    "slowPower": legacy.get("slow_power") or "",
                    "fastPrices": fast_prices,
                    "slowPrices": slow_prices,
                    "facilities": facilities,
                    "tags": tags,
                    "favoriteCount": legacy.get("favorite_count") or "",
                    "collectedAt": incoming_time,
                }
                task_id = str(task.get("local_task_id") or "")[:40] if task else ""
                source_address = str(task.get("address") or "") if task else str(legacy.get("address") or "")
                source_latitude = task.get("latitude") if task else legacy.get("latitude")
                source_longitude = task.get("longitude") if task else legacy.get("longitude")
                observation_id = f"henan-migrated-{legacy['id']}-{key[:12]}"
                created_at = int(legacy.get("created_at") or incoming_time or 0)
                updated_at = int(legacy.get("updated_at") or incoming_time or 0)
                values = {
                    "source_key": key,
                    "observation_id": observation_id,
                    "task_id": task_id,
                    "device_id": str(current.get("device_id") or "") if current else "",
                    "collection_source": "HENAN_POI",
                    "province": str(legacy.get("province") or "河南省"),
                    "city": str(legacy.get("city") or (task.get("city") if task else "") or ""),
                    "district": str(task.get("district") or "") if task else "",
                    "source_site_id": str(current.get("source_site_id") or "") if current else "",
                    "source_station_id": station_id or (str(current.get("source_station_id") or "") if current else ""),
                    "requested_name": str(task.get("station_name") or name) if task else name,
                    "matched_station_name": name,
                    "source_address": source_address,
                    "collected_address": str(legacy.get("address") or ""),
                    "source_latitude": source_latitude or 0,
                    "source_longitude": source_longitude or 0,
                    "operator": str(legacy.get("operator") or ""),
                    "business_hours": str(legacy.get("business_hours") or ""),
                    "current_price": str(legacy.get("current_price") or ""),
                    "parking_fee": str(legacy.get("parking_fee") or ""),
                    "fast_available": str(legacy.get("fast_available") or ""),
                    "fast_total": str(legacy.get("fast_total") or ""),
                    "fast_power": str(legacy.get("fast_power") or ""),
                    "super_available": str(legacy.get("super_available") or ""),
                    "super_total": str(legacy.get("super_total") or ""),
                    "super_power": str(legacy.get("super_power") or ""),
                    "slow_available": str(legacy.get("slow_available") or ""),
                    "slow_total": str(legacy.get("slow_total") or ""),
                    "slow_power": str(legacy.get("slow_power") or ""),
                    "result_payload": json.dumps(payload, ensure_ascii=False),
                    "captured_at": incoming_time,
                    "received_at": updated_at,
                    "created_at": created_at,
                    "updated_at": updated_at,
                }
                columns = list(values)
                if current is None:
                    cursor.execute(
                        f"INSERT INTO `{TARGET}` ({','.join(columns)}) VALUES ({','.join('%s' for _ in columns)})",
                        tuple(values[column] for column in columns),
                    )
                    values["id"] = cursor.lastrowid
                    current_by_key[key] = values
                    inserted += 1
                else:
                    assignments = ",".join(f"{column}=%s" for column in columns if column != "source_key")
                    update_columns = [column for column in columns if column != "source_key"]
                    cursor.execute(
                        f"UPDATE `{TARGET}` SET {assignments} WHERE id=%s",
                        tuple(values[column] for column in update_columns) + (current["id"],),
                    )
                    values["id"] = current["id"]
                    current_by_key[key] = values
                    overwritten += 1
            conn.commit()

            cursor.execute(f"SELECT COUNT(*) AS total, COUNT(DISTINCT source_key) AS unique_keys FROM `{TARGET}`")
            verification = cursor.fetchone()
            if verification["total"] != verification["unique_keys"]:
                raise RuntimeError(f"统一结果表仍有重复键: {verification}")
            if verification["total"] != expected_max:
                raise RuntimeError(
                    f"统一结果数量不符合预期: actual={verification['total']} expected={expected_max}"
                )
            cursor.execute(f"DROP TABLE `{LEGACY}`")
            conn.commit()

        print(json.dumps({
            "backup": str(backup_path),
            "unifiedRows": verification["total"],
            "legacyInserted": inserted,
            "legacyOverwrittenLatest": overwritten,
            "newerUnifiedRowsKept": kept_newer,
            "legacyTableDropped": True,
        }, ensure_ascii=False, indent=2))
    finally:
        conn.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    main(args.apply)
