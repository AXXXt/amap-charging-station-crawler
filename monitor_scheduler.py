"""Rolling hourly monitor scheduler for selected charging stations.

This module is intentionally isolated from the normal Henan task table. It
creates its own batches, rounds and tasks, so the existing full-site pipeline
keeps its original behavior.
"""
from __future__ import annotations

import json
import math
import os
import re
import secrets
import uuid
from datetime import datetime, timedelta, timezone

import pymysql
import priority_station_scopes
import cross_platform_monitor as supplements


def _env_int(name: str, fallback: int, allow_zero: bool = False) -> int:
    try:
        value = int(os.getenv(name, "") or fallback)
    except (TypeError, ValueError):
        return fallback
    if value == 0 and allow_zero:
        return 0
    return value if value > 0 else fallback


def _env_flag(name: str, fallback: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return fallback
    return raw.strip().lower() not in {"0", "false", "no", "off"}


TASK_TYPE = "HENAN_POI_DETAIL"
# 全库模式（默认开启）：把结果表里所有站点装入同一个批次并无限循环，
# 跑完一轮立即开始下一轮（轮次间隔 0 分钟）。用 MONITOR_FULL_LIBRARY=0 可关掉。
FULL_LIBRARY_MODE = _env_flag("MONITOR_FULL_LIBRARY", True)
# size=0 表示"不限制"（全库）。经典模式默认 400。
DEFAULT_BATCH_SIZE = _env_int("MONITOR_BATCH_SIZE", 0 if FULL_LIBRARY_MODE else 400, allow_zero=True)
# rounds=0 表示"无限循环，不自动结束批次"。
DEFAULT_TARGET_ROUNDS = _env_int("MONITOR_TARGET_ROUNDS", 0 if FULL_LIBRARY_MODE else 24, allow_zero=True)
# interval=0 表示"上一轮跑完立即开始下一轮"。
DEFAULT_INTERVAL_MINUTES = _env_int("MONITOR_INTERVAL_MINUTES", 0 if FULL_LIBRARY_MODE else 60, allow_zero=True)
LEASE_SECONDS = 900
# First attempt plus at most one retry per station and round.
MAX_ATTEMPTS = 2


class MonitorTaskError(Exception):
    """Raised when a monitor task lease or task lookup is invalid."""


def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _as_utc(value):
    if isinstance(value, datetime):
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed

def _conn(db_config):
    config = dict(db_config)
    config.pop("cursorclass", None)
    return pymysql.connect(
        **config,
        cursorclass=pymysql.cursors.DictCursor,
        autocommit=False,
        read_timeout=30,
        write_timeout=30,
    )


def _add_column_if_missing(cursor, table, column, definition):
    cursor.execute(
        "SELECT COUNT(*) AS count FROM information_schema.columns "
        "WHERE table_schema=DATABASE() AND table_name=%s AND column_name=%s",
        (table, column),
    )
    if int(cursor.fetchone()["count"]) == 0:
        try:
            cursor.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
        except pymysql.err.OperationalError as error:
            # Multiple API workers can run the same startup migration together.
            if error.args[0] != 1060:
                raise

def init_tables(db_config):
    ddl = [
        """
        CREATE TABLE IF NOT EXISTS station_monitor_batch (
            id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
            batch_code VARCHAR(32) COLLATE utf8mb4_general_ci NOT NULL,
            name VARCHAR(128) COLLATE utf8mb4_general_ci NOT NULL DEFAULT '',
            status VARCHAR(16) COLLATE utf8mb4_general_ci NOT NULL DEFAULT 'PENDING',
            station_count INT UNSIGNED NOT NULL DEFAULT 0,
            target_rounds INT UNSIGNED NOT NULL DEFAULT 24,
            target_interval_minutes INT UNSIGNED NOT NULL DEFAULT 60,
            current_round INT UNSIGNED NOT NULL DEFAULT 0,
            collection_mode VARCHAR(16) COLLATE utf8mb4_general_ci NOT NULL DEFAULT 'DETAIL',
            scope_key VARCHAR(64) COLLATE utf8mb4_general_ci NOT NULL DEFAULT '',
            started_at DATETIME NULL,
            finished_at DATETIME NULL,
            created_at DATETIME NOT NULL,
            updated_at DATETIME NOT NULL,
            PRIMARY KEY (id),
            UNIQUE KEY uk_monitor_batch_code (batch_code),
            KEY idx_monitor_batch_status (status)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci
        """,
        """
        CREATE TABLE IF NOT EXISTS station_monitor_batch_station (
            id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
            batch_id BIGINT UNSIGNED NOT NULL,
            source_key CHAR(64) COLLATE utf8mb4_general_ci NOT NULL,
            amap_poi_id VARCHAR(64) COLLATE utf8mb4_general_ci NOT NULL DEFAULT '',
            station_name VARCHAR(512) COLLATE utf8mb4_general_ci NOT NULL DEFAULT '',
            city VARCHAR(128) COLLATE utf8mb4_general_ci NOT NULL DEFAULT '',
            district VARCHAR(128) COLLATE utf8mb4_general_ci NOT NULL DEFAULT '',
            station_address VARCHAR(1000) COLLATE utf8mb4_general_ci NOT NULL DEFAULT '',
            latitude DECIMAL(12,8) NULL,
            longitude DECIMAL(12,8) NULL,
            sequence_no INT UNSIGNED NOT NULL DEFAULT 0,
            created_at DATETIME NOT NULL,
            PRIMARY KEY (id),
            UNIQUE KEY uk_monitor_batch_station (batch_id, source_key),
            KEY idx_monitor_batch_station_source (source_key),
            KEY idx_monitor_batch_station_city (city)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci
        """,
        """
        CREATE TABLE IF NOT EXISTS station_monitor_round (
            id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
            batch_id BIGINT UNSIGNED NOT NULL,
            round_no INT UNSIGNED NOT NULL,
            status VARCHAR(16) COLLATE utf8mb4_general_ci NOT NULL DEFAULT 'RUNNING',
            planned_at DATETIME NULL,
            started_at DATETIME NULL,
            finished_at DATETIME NULL,
            completed_count INT UNSIGNED NOT NULL DEFAULT 0,
            failed_count INT UNSIGNED NOT NULL DEFAULT 0,
            created_at DATETIME NOT NULL,
            updated_at DATETIME NOT NULL,
            PRIMARY KEY (id),
            UNIQUE KEY uk_monitor_round (batch_id, round_no),
            KEY idx_monitor_round_status (batch_id, status)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci
        """,
        """
        CREATE TABLE IF NOT EXISTS station_monitor_task (
            id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
            task_key CHAR(64) COLLATE utf8mb4_general_ci NOT NULL,
            local_task_id VARCHAR(64) COLLATE utf8mb4_general_ci NOT NULL,
            batch_id BIGINT UNSIGNED NOT NULL,
            round_id BIGINT UNSIGNED NOT NULL,
            round_no INT UNSIGNED NOT NULL,
            source_key CHAR(64) COLLATE utf8mb4_general_ci NOT NULL,
            amap_poi_id VARCHAR(64) COLLATE utf8mb4_general_ci NOT NULL DEFAULT '',
            station_name VARCHAR(512) COLLATE utf8mb4_general_ci NOT NULL DEFAULT '',
            station_address VARCHAR(1000) COLLATE utf8mb4_general_ci NOT NULL DEFAULT '',
            city VARCHAR(128) COLLATE utf8mb4_general_ci NOT NULL DEFAULT '',
            district VARCHAR(128) COLLATE utf8mb4_general_ci NOT NULL DEFAULT '',
            latitude DECIMAL(12,8) NULL,
            longitude DECIMAL(12,8) NULL,
            sequence_no INT UNSIGNED NOT NULL DEFAULT 0,
            priority INT UNSIGNED NOT NULL DEFAULT 80,
            collection_mode VARCHAR(16) COLLATE utf8mb4_general_ci NOT NULL DEFAULT 'DETAIL',
            type VARCHAR(32) COLLATE utf8mb4_general_ci NOT NULL DEFAULT 'HENAN_POI_DETAIL',
            status VARCHAR(16) COLLATE utf8mb4_general_ci NOT NULL DEFAULT 'PENDING',
            attempt SMALLINT UNSIGNED NOT NULL DEFAULT 0,
            max_attempts SMALLINT UNSIGNED NOT NULL DEFAULT 2,
            lease_device_id VARCHAR(64) COLLATE utf8mb4_general_ci NOT NULL DEFAULT '',
            lease_token VARCHAR(64) COLLATE utf8mb4_general_ci NOT NULL DEFAULT '',
            lease_expires_at DATETIME NULL,
            available_at DATETIME NOT NULL,
            started_at DATETIME NULL,
            finished_at DATETIME NULL,
            last_error VARCHAR(1000) COLLATE utf8mb4_general_ci NOT NULL DEFAULT '',
            progress JSON NULL,
            result_summary JSON NULL,
            created_at DATETIME NOT NULL,
            updated_at DATETIME NOT NULL,
            PRIMARY KEY (id),
            UNIQUE KEY uk_monitor_task_key (task_key),
            UNIQUE KEY uk_monitor_task_local (local_task_id),
            UNIQUE KEY uk_monitor_round_station (round_id, source_key),
            KEY idx_monitor_task_claim (round_id, status, available_at, attempt, id),
            KEY idx_monitor_task_source (source_key, id)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci
        """,
    ]
    conn = _conn(db_config)
    try:
        with conn.cursor() as cursor:
            for statement in ddl:
                cursor.execute(statement)
            _add_column_if_missing(cursor, "station_monitor_batch", "collection_mode", "VARCHAR(16) COLLATE utf8mb4_general_ci NOT NULL DEFAULT 'DETAIL'")
            _add_column_if_missing(cursor, "station_monitor_batch", "scope_key", "VARCHAR(64) COLLATE utf8mb4_general_ci NOT NULL DEFAULT ''")
            _add_column_if_missing(cursor, "station_monitor_task", "collection_mode", "VARCHAR(16) COLLATE utf8mb4_general_ci NOT NULL DEFAULT 'DETAIL'")
            cursor.execute(supplements.CREATE_TABLE_SQL)
            _apply_retry_limit(cursor, _now())
        conn.commit()
    finally:
        conn.close()


def _apply_retry_limit(cursor, now, batch_ids=None):
    """Cap unfinished legacy tasks without resetting attempts or interrupting leases."""
    batch_filter = ""
    params = []
    if batch_ids is not None:
        if not batch_ids:
            return {"updated": 0, "failed": 0}
        batch_filter = " AND id IN (" + ",".join(["%s"] * len(batch_ids)) + ")"
        params.extend(batch_ids)
    # Read batch metadata without locking it: only affected task rows need locks.
    cursor.execute(
        "SELECT id, current_round FROM station_monitor_batch WHERE status='ACTIVE'"
        + batch_filter + " ORDER BY id",
        tuple(params),
    )
    batches = cursor.fetchall()
    rows = []
    for batch in batches:
        cursor.execute(
            "SELECT id, round_id, status, attempt, max_attempts FROM station_monitor_task "
            "WHERE batch_id=%s AND round_no=%s AND status IN ('PENDING','LEASED','RUNNING') "
            "AND (max_attempts>%s OR (status='PENDING' AND attempt>=max_attempts)) "
            "ORDER BY id FOR UPDATE",
            (batch["id"], batch["current_round"], MAX_ATTEMPTS),
        )
        rows.extend(cursor.fetchall())
    failed_by_round = {}
    for row in rows:
        limit = min(int(row["max_attempts"]), MAX_ATTEMPTS)
        exhausted = row["status"] == "PENDING" and int(row["attempt"]) >= limit
        if exhausted:
            cursor.execute(
                "UPDATE station_monitor_task SET max_attempts=%s, status='FAILED', "
                "finished_at=%s, updated_at=%s WHERE id=%s",
                (limit, now, now, row["id"]),
            )
            round_id = row["round_id"]
            failed_by_round[round_id] = failed_by_round.get(round_id, 0) + 1
        else:
            cursor.execute(
                "UPDATE station_monitor_task SET max_attempts=%s, updated_at=%s WHERE id=%s",
                (limit, now, row["id"]),
            )
    for round_id, count in failed_by_round.items():
        cursor.execute(
            "UPDATE station_monitor_round SET failed_count=failed_count+%s, updated_at=%s WHERE id=%s",
            (count, now, round_id),
        )
    return {"updated": len(rows), "failed": sum(failed_by_round.values())}


def _json_object(value):
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except (TypeError, ValueError):
            return {}
    return {}


def _float_or_none(value):
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def task_payload(row):
    latitude = _float_or_none(row.get("latitude"))
    longitude = _float_or_none(row.get("longitude"))
    source_payload = {
        "id": row.get("amap_poi_id") or "",
        "name": row.get("station_name") or "",
        "address": row.get("station_address") or "",
        "latitude": latitude,
        "longitude": longitude,
    }
    return {
        "id": row["local_task_id"],
        "type": row.get("type") or TASK_TYPE,
        "priority": int(row.get("priority") or 80),
        "province": "河南省",
        "city": row.get("city") or "",
        "district": row.get("district") or "",
        "keyword": row.get("station_name") or "",
        "searchRegion": "",
        "leaseToken": row.get("lease_token") or "",
        "attempt": int(row.get("attempt") or 0),
        "maxAttempts": int(row.get("max_attempts") or MAX_ATTEMPTS),
        "recoveryAttempt": 0,
        "maxRecoveryAttempts": 0 if row.get("_supplement") else 2,
        "sourceSiteId": "",
        "sourceSiteOrder": 0,
        "stationId": row.get("amap_poi_id") or "",
        "stationName": row.get("station_name") or "",
        "stationAddress": row.get("station_address") or "",
        "stationLatitude": latitude,
        "stationLongitude": longitude,
        "stationSequence": int(row.get("sequence_no") or 0),
        "sourcePayload": source_payload,
        "collectionMode": row.get("collection_mode") or "DETAIL",
        **({"crossPlatform": supplements.metadata(row)} if row.get("_supplement") else {}),
    }


def legacy_task(row):
    """Return a row shaped like the original authoritative Henan task row."""
    payload = task_payload(row)
    return {
        "id": row["local_task_id"],
        "type": row.get("type") or TASK_TYPE,
        "priority": payload["priority"],
        "province": payload["province"],
        "city": payload["city"],
        "district": payload["district"],
        "keyword": payload["keyword"],
        "search_region": "",
        "status": row.get("status") or "PENDING",
        "assigned_device_id": row.get("lease_device_id") or "",
        "lease_token": row.get("lease_token") or "",
        "lease_expires_at": row.get("lease_expires_at"),
        "attempt": int(row.get("attempt") or 0),
        "max_attempts": int(row.get("max_attempts") or MAX_ATTEMPTS),
        "recovery_attempt": 0,
        "max_recovery_attempts": 2,
        "progress": _json_object(row.get("progress")),
        "result_summary": _json_object(row.get("result_summary")),
        "available_at": row.get("available_at"),
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
        "started_at": row.get("started_at"),
        "finished_at": row.get("finished_at"),
        "last_error": row.get("last_error") or "",
        "source_station_id": payload["stationId"],
        "source_payload": payload["sourcePayload"],
        "source_sequence": payload["stationSequence"],
        "collection_mode": row.get("collection_mode") or "DETAIL",
        **({"cross_platform": supplements.metadata(row)} if row.get("_supplement") else {}),
    }


def get_task(db_config, task_id):
    row = _get_row(db_config, task_id)
    return legacy_task(row) if row else None


def _reap_expired(db_config):
    now = _now()
    conn = _conn(db_config)
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                "UPDATE station_monitor_task "
                "SET status='PENDING', lease_device_id='', lease_token='', lease_expires_at=NULL, "
                "available_at=%s, last_error='LEASE_EXPIRED', updated_at=%s "
                "WHERE status IN ('LEASED','RUNNING') AND lease_expires_at IS NOT NULL "
                "AND lease_expires_at < %s AND attempt < max_attempts",
                (now, now, now),
            )
            cursor.execute(
                "UPDATE station_monitor_task "
                "SET status='FAILED', lease_device_id='', lease_token='', lease_expires_at=NULL, "
                "finished_at=%s, last_error='LEASE_EXPIRED', updated_at=%s "
                "WHERE status IN ('LEASED','RUNNING') AND lease_expires_at IS NOT NULL "
                "AND lease_expires_at < %s AND attempt >= max_attempts",
                (now, now, now),
            )
        conn.commit()
    finally:
        conn.close()


def _active_batch_id(db_config, scope_key=""):
    conn = _conn(db_config)
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                "SELECT id FROM station_monitor_batch WHERE status='ACTIVE' "
                "AND scope_key=%s ORDER BY id LIMIT 1",
                (scope_key,),
            )
            row = cursor.fetchone()
        return int(row["id"]) if row else None
    finally:
        conn.close()


_TRAILING_ELLIPSIS_RE = re.compile(r"[.．。…]+\s*$")


def _strip_rendered_ellipsis(value: str | None) -> str:
    return _TRAILING_ELLIPSIS_RE.sub("", (value or "").strip()).strip()


def canonical_station_name(matched_name: str | None, requested_name: str | None) -> str:
    """Choose the fuller station name without copying rendered truncation.

    The monitor batch is built from the unified result table. Older list
    snapshots could store AMap's rendered card title (for example a title
    ending in "...") as ``matched_station_name`` while ``requested_name``
    kept the full search query. Prefer the longer name whenever one cleaned
    name is a prefix of the other so future rounds do not inherit the
    truncation and fail with NO_MATCH.
    """
    matched = (matched_name or "").strip()
    requested = (requested_name or "").strip()
    if not matched:
        return requested
    if not requested:
        return matched
    clean_matched = _strip_rendered_ellipsis(matched)
    clean_requested = _strip_rendered_ellipsis(requested)

    if clean_requested.startswith(clean_matched) and len(clean_requested) > len(clean_matched):
        return requested
    if clean_matched.startswith(clean_requested) and len(clean_matched) > len(clean_requested):
        return matched

    common_prefix = len(os.path.commonprefix([clean_matched, clean_requested]))
    matched_truncated = matched != clean_matched
    requested_truncated = requested != clean_requested
    if common_prefix >= 8 and (matched_truncated or requested_truncated):
        if requested_truncated and not matched_truncated:
            return matched
        if matched_truncated and not requested_truncated:
            return requested
        return matched if len(clean_matched) >= len(clean_requested) else requested
    return matched


def _canonicalize_candidate_rows(rows):
    for row in rows:
        row["matched_station_name"] = canonical_station_name(
            row.get("matched_station_name"),
            row.get("requested_name"),
        )
    return rows


def _candidate_rows(conn, size, city):
    city_filter = "AND r.city=%s" if city else ""
    args = ([city] if city else []) + [size]
    if city:
        query = f"""
            SELECT r.source_key, r.source_station_id, r.matched_station_name, r.requested_name,
                   r.city, r.district,
                   COALESCE(NULLIF(r.collected_address,''), r.source_address, '') AS station_address,
                   r.source_latitude AS latitude, r.source_longitude AS longitude
              FROM site_exploration_charging_station_result r
             WHERE r.source_key <> ''
               AND NULLIF(TRIM(r.current_price),'') IS NOT NULL
               AND r.source_latitude IS NOT NULL AND r.source_longitude IS NOT NULL
               AND r.source_latitude <> 0 AND r.source_longitude <> 0
               AND COALESCE(JSON_LENGTH(JSON_EXTRACT(r.result_payload,'$.chargingPiles')),0) > 0
               {city_filter}
               AND NOT EXISTS (
                   SELECT 1 FROM station_monitor_batch_station bs WHERE bs.source_key=r.source_key
               )
             ORDER BY CRC32(r.source_key)
             LIMIT %s
        """
    else:
        query = f"""
            WITH ranked AS (
                SELECT r.source_key, r.source_station_id, r.matched_station_name, r.requested_name,
                       r.city, r.district,
                       COALESCE(NULLIF(r.collected_address,''), r.source_address, '') AS station_address,
                       r.source_latitude AS latitude, r.source_longitude AS longitude,
                       ROW_NUMBER() OVER (PARTITION BY r.city ORDER BY CRC32(r.source_key)) AS rn
                  FROM site_exploration_charging_station_result r
                 WHERE r.source_key <> ''
                   AND NULLIF(TRIM(r.current_price),'') IS NOT NULL
                   AND r.source_latitude IS NOT NULL AND r.source_longitude IS NOT NULL
                   AND r.source_latitude <> 0 AND r.source_longitude <> 0
                   AND COALESCE(JSON_LENGTH(JSON_EXTRACT(r.result_payload,'$.chargingPiles')),0) > 0
                   AND NOT EXISTS (
                       SELECT 1 FROM station_monitor_batch_station bs WHERE bs.source_key=r.source_key
                   )
            )
            SELECT source_key, source_station_id, matched_station_name, requested_name,
                   city, district, station_address, latitude, longitude
              FROM ranked
             WHERE rn <= 8
             ORDER BY city, rn
             LIMIT %s
        """
    with conn.cursor() as cursor:
        cursor.execute(query, args)
        return _canonicalize_candidate_rows(cursor.fetchall())


def _all_station_candidates(conn, size):
    """全库模式：装入结果表里所有有名字的站点（size<=0 表示不限制）。"""
    query = """
        SELECT r.source_key,
               r.source_station_id,
               r.matched_station_name,
               r.requested_name,
               r.city, r.district,
               COALESCE(NULLIF(r.collected_address,''), r.source_address, '') AS station_address,
               r.source_latitude AS latitude,
               r.source_longitude AS longitude
          FROM site_exploration_charging_station_result r
         WHERE r.source_key <> ''
           AND TRIM(COALESCE(NULLIF(r.matched_station_name,''), r.requested_name)) <> ''
         ORDER BY r.city, r.district, r.source_key
    """
    with conn.cursor() as cursor:
        if size and int(size) > 0:
            cursor.execute(query + " LIMIT %s", (int(size),))
        else:
            cursor.execute(query)
        return _canonicalize_candidate_rows(cursor.fetchall())


def _reuse_candidates(conn, size, reuse_batch_ids):
    """复用已有批次里的站点（多批合并、按 source_key 去重）。"""
    ids = sorted({int(value) for value in reuse_batch_ids})
    if not ids:
        return []
    placeholders = ", ".join(["%s"] * len(ids))
    query = f"""
        SELECT bs.source_key,
               bs.amap_poi_id AS source_station_id,
               bs.station_name AS matched_station_name,
               r.requested_name,
               bs.city, bs.district,
               bs.station_address,
               bs.latitude, bs.longitude
          FROM station_monitor_batch_station bs
          JOIN (
                SELECT source_key, MIN(batch_id) AS first_batch
                  FROM station_monitor_batch_station
                 WHERE batch_id IN ({placeholders})
                 GROUP BY source_key
               ) picked
            ON picked.source_key = bs.source_key AND picked.first_batch = bs.batch_id
          LEFT JOIN site_exploration_charging_station_result r
            ON r.source_key = bs.source_key
         ORDER BY bs.batch_id, bs.sequence_no
         LIMIT %s
    """
    with conn.cursor() as cursor:
        cursor.execute(query, tuple(ids) + (int(size),))
        return _canonicalize_candidate_rows(cursor.fetchall())


def close_active_batches(db_config):
    """把当前所有 ACTIVE 批次标记为完成，便于新建批次接管。"""
    now = _now()
    conn = _conn(db_config)
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                "UPDATE station_monitor_batch SET status='COMPLETED', finished_at=%s, updated_at=%s "
                "WHERE status='ACTIVE'",
                (now, now),
            )
            affected = cursor.rowcount
        conn.commit()
        return affected
    finally:
        conn.close()


def create_batch(db_config, size=DEFAULT_BATCH_SIZE, target_rounds=DEFAULT_TARGET_ROUNDS,
                 interval_minutes=DEFAULT_INTERVAL_MINUTES, city="", collection_mode="DETAIL",
                 reuse_batch_ids=None, all_stations=None, scope_key=""):
    init_tables(db_config)
    now = _now()
    code = "MON-" + datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S") + "-" + secrets.token_hex(3)
    conn = _conn(db_config)
    try:
        if all_stations is None:
            all_stations = FULL_LIBRARY_MODE
        if scope_key:
            scope = priority_station_scopes.load_scopes()[scope_key]
            collection_mode = scope["collection_mode"]
            target_rounds = 0
            interval_minutes = 0
            candidates = priority_station_scopes.candidate_rows(conn, scope_key)
        elif reuse_batch_ids:
            candidates = _reuse_candidates(conn, size, reuse_batch_ids)
        elif all_stations:
            candidates = _all_station_candidates(conn, size)
        else:
            candidates = _candidate_rows(conn, size, city.strip())
        if not candidates:
            raise RuntimeError("NO_ELIGIBLE_MONITOR_STATIONS")
        with conn.cursor() as cursor:
            cursor.execute(
                "INSERT INTO station_monitor_batch "
                "(batch_code,name,status,station_count,target_rounds,target_interval_minutes,collection_mode,scope_key,created_at,updated_at) "
                "VALUES (%s,%s,'ACTIVE',%s,%s,%s,%s,%s,%s,%s)",
                (
                    code,
                    f"{scope_key or '重点站点监控批次'} {code}",
                    len(candidates),
                    target_rounds,
                    interval_minutes,
                    collection_mode,
                    scope_key,
                    now,
                    now,
                ),
            )
            batch_id = int(cursor.lastrowid)
            for index, row in enumerate(candidates, start=1):
                cursor.execute(
                    "INSERT INTO station_monitor_batch_station "
                    "(batch_id,source_key,amap_poi_id,station_name,city,district,station_address,latitude,longitude,sequence_no,created_at) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                    (
                        batch_id,
                        row["source_key"],
                        row.get("source_station_id") or "",
                        row.get("matched_station_name") or "",
                        row.get("city") or "",
                        row.get("district") or "",
                        row.get("station_address") or "",
                        row.get("latitude"),
                        row.get("longitude"),
                        index,
                        now,
                    ),
                )
        conn.commit()
        return batch_id
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _ensure_active_batch(db_config, scope_key=""):
    lock_name = "station_monitor_batch:" + (scope_key or "legacy")
    lock_conn = _conn(db_config)
    try:
        with lock_conn.cursor() as cursor:
            cursor.execute("SELECT GET_LOCK(%s, 30) AS got", (lock_name,))
            got = cursor.fetchone()
            if not got or int(got.get("got") or 0) != 1:
                raise RuntimeError("MONITOR_BATCH_LOCK_TIMEOUT")
        batch_id = _active_batch_id(db_config, scope_key)
        if batch_id:
            return batch_id
        return create_batch(db_config, scope_key=scope_key)
    finally:
        try:
            with lock_conn.cursor() as cursor:
                cursor.execute("SELECT RELEASE_LOCK(%s)", (lock_name,))
        finally:
            lock_conn.close()


def _start_next_round(db_config, batch_id):
    now = _now()
    conn = _conn(db_config)
    try:
        conn.begin()
        with conn.cursor() as cursor:
            cursor.execute(
                "SELECT * FROM station_monitor_batch WHERE id=%s FOR UPDATE",
                (batch_id,),
            )
            batch = cursor.fetchone()
            if not batch or batch["status"] != "ACTIVE":
                conn.commit()
                return None
            current_round = int(batch["current_round"] or 0)
            next_available_at = now
            if current_round:
                cursor.execute(
                    "SELECT * FROM station_monitor_round WHERE batch_id=%s AND round_no=%s",
                    (batch_id, current_round),
                )
                current_round_row = cursor.fetchone()
                if current_round_row:
                    cursor.execute(
                        "SELECT COUNT(*) AS active_count FROM station_monitor_task "
                        "WHERE round_id=%s AND status IN ('PENDING','LEASED','RUNNING')",
                        (current_round_row["id"],),
                    )
                    if int(cursor.fetchone()["active_count"] or 0) > 0:
                        conn.commit()
                        return current_round
                    if supplements.has_active(cursor, current_round_row["id"], now):
                        conn.commit()
                        return current_round
                    cursor.execute(
                        "UPDATE station_monitor_round SET status='COMPLETED', finished_at=%s, updated_at=%s "
                        "WHERE id=%s AND status<>'COMPLETED'",
                        (now, now, current_round_row["id"]),
                    )
                if current_round and current_round_row and current_round_row.get("started_at"):
                    interval = batch.get("target_interval_minutes")
                    interval_minutes = int(DEFAULT_INTERVAL_MINUTES if interval is None else interval)
                    target_at = _as_utc(current_round_row["started_at"]) + timedelta(minutes=interval_minutes)
                    if target_at > _as_utc(now):
                        next_available_at = target_at.strftime("%Y-%m-%d %H:%M:%S")
                target_rounds = int(batch["target_rounds"] or 0)
                if target_rounds > 0 and current_round >= target_rounds:
                    cursor.execute(
                        "UPDATE station_monitor_batch SET status='COMPLETED', finished_at=%s, updated_at=%s WHERE id=%s",
                        (now, now, batch_id),
                    )
                    conn.commit()
                    return None
                next_round = current_round + 1
            else:
                next_round = 1
            cursor.execute(
                "INSERT INTO station_monitor_round "
                "(batch_id,round_no,status,planned_at,started_at,created_at,updated_at) "
                "VALUES (%s,%s,'RUNNING',%s,%s,%s,%s)",
                (batch_id, next_round, now, now, now, now),
            )
            round_id = int(cursor.lastrowid)
            cursor.execute(
                "INSERT INTO station_monitor_task "
                "(task_key,local_task_id,batch_id,round_id,round_no,source_key,amap_poi_id,"
                "station_name,station_address,city,district,latitude,longitude,sequence_no,"
                "priority,collection_mode,type,status,attempt,max_attempts,available_at,created_at,updated_at) "
                "SELECT SHA2(CONCAT(%s,':',%s,':',bs.source_key),256), UUID(), bs.batch_id, %s, %s, "
                "bs.source_key, bs.amap_poi_id, bs.station_name, bs.station_address, bs.city, bs.district, "
                "bs.latitude, bs.longitude, bs.sequence_no, 80, "
                "CASE WHEN b.collection_mode='LIST' THEN 'LIST' ELSE 'DETAIL' END, "
                "CASE WHEN b.collection_mode='LIST' THEN 'HENAN_POI_LIST' ELSE 'HENAN_POI_DETAIL' END, "
                "'PENDING', 0, %s, %s, %s, %s "
                "FROM station_monitor_batch_station bs "
                "JOIN station_monitor_batch b ON b.id=bs.batch_id "
                "WHERE bs.batch_id=%s",
                (batch_id, next_round, round_id, next_round, MAX_ATTEMPTS, next_available_at, now, now, batch_id),
            )
            cursor.execute(
                "UPDATE station_monitor_batch SET current_round=%s, started_at=COALESCE(started_at,%s), updated_at=%s WHERE id=%s",
                (next_round, now, now, batch_id),
            )
        conn.commit()
        return next_round
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def claim(db_config, device, scope_key=""):
    init_tables(db_config)
    _reap_expired(db_config)
    if scope_key in supplements.SCOPES:
        conn = _conn(db_config)
        try:
            with conn.cursor() as cursor:
                extra = supplements.claim(cursor, device, scope_key, _now(), secrets.token_urlsafe(24))
            conn.commit()
            if extra:
                return task_payload(extra), "CROSS_PLATFORM_CLAIMED"
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
    try:
        batch_id = _ensure_active_batch(db_config, scope_key)
    except RuntimeError as error:
        if str(error) == "NO_ELIGIBLE_MONITOR_STATIONS":
            return None, "ALL_BATCHES_COMPLETED"
        raise
    for _ in range(4):
        round_no = _start_next_round(db_config, batch_id)
        if round_no is None:
            return None, "QUEUE_EMPTY"
        now = _now()
        conn = _conn(db_config)
        try:
            conn.begin()
            with conn.cursor() as cursor:
                cursor.execute(
                    "SELECT t.*, b.collection_mode "
                    "FROM station_monitor_task t JOIN station_monitor_batch b ON b.id=t.batch_id "
                    "WHERE t.batch_id=%s AND t.round_no=%s AND t.status='PENDING' "
                    "AND available_at<=%s AND attempt<max_attempts "
                    "ORDER BY attempt, id LIMIT 1 FOR UPDATE SKIP LOCKED",
                    (batch_id, round_no, now),
                )
                row = cursor.fetchone()
                if row is None:
                    cursor.execute(
                        "SELECT COUNT(*) AS active_count FROM station_monitor_task "
                        "WHERE round_id=(SELECT id FROM station_monitor_round WHERE batch_id=%s AND round_no=%s LIMIT 1) "
                        "AND status IN ('PENDING','LEASED','RUNNING')",
                        (batch_id, round_no),
                    )
                    active = int(cursor.fetchone()["active_count"] or 0)
                    conn.commit()
                    if active == 0:
                        continue
                    return None, "QUEUE_EMPTY"
                token = secrets.token_urlsafe(24)
                expires_at = (
                    datetime.now(timezone.utc) + timedelta(seconds=LEASE_SECONDS)
                ).strftime("%Y-%m-%d %H:%M:%S")
                cursor.execute(
                    "UPDATE station_monitor_task SET status='LEASED', lease_device_id=%s, "
                    "lease_token=%s, lease_expires_at=%s, attempt=attempt+1, "
                    "started_at=COALESCE(started_at,%s), updated_at=%s WHERE id=%s",
                    (device["id"], token, expires_at, now, now, row["id"]),
                )
                row["status"] = "LEASED"
                row["lease_device_id"] = device["id"]
                row["lease_token"] = token
                row["lease_expires_at"] = expires_at
                row["attempt"] = int(row.get("attempt") or 0) + 1
                conn.commit()
                return task_payload(row), "MONITOR_CLAIMED"
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
    return None, "QUEUE_EMPTY"


def _locked_row(cursor, task_id):
    if task_id.startswith(supplements.PREFIX):
        return supplements.get_row(cursor, task_id, lock=True)
    cursor.execute("SELECT * FROM station_monitor_task WHERE local_task_id=%s LIMIT 1 FOR UPDATE", (task_id,))
    return cursor.fetchone()


def _get_row(db_config, task_id):
    conn = _conn(db_config)
    try:
        with conn.cursor() as cursor:
            if task_id.startswith(supplements.PREFIX):
                return supplements.get_row(cursor, task_id)
            cursor.execute("SELECT * FROM station_monitor_task WHERE local_task_id=%s LIMIT 1", (task_id,))
            return cursor.fetchone()
    finally:
        conn.close()


def _assert_lease(row, device_id, lease_token):
    if row is None:
        raise MonitorTaskError("TASK_NOT_FOUND")
    if row["status"] not in {"LEASED", "RUNNING"}:
        raise MonitorTaskError("TASK_NOT_ACTIVE")
    if row["lease_device_id"] != device_id or row["lease_token"] != lease_token:
        raise MonitorTaskError("TASK_LEASE_STALE")
    expires_at = row.get("lease_expires_at")
    if not expires_at:
        raise MonitorTaskError("TASK_LEASE_EXPIRED")
    if isinstance(expires_at, datetime):
        expires = expires_at.replace(tzinfo=timezone.utc) if expires_at.tzinfo is None else expires_at
    else:
        expires = datetime.fromisoformat(str(expires_at).replace("Z", "+00:00"))
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
    if expires < datetime.now(timezone.utc):
        raise MonitorTaskError("TASK_LEASE_EXPIRED")



def release(db_config, task_id, device_id, lease_token):
    """Return an interrupted lease without failure, transfer, or retry consumption.

    Ownership is checked under the same row lock as the update. An expired
    lease may still be returned by its owner until the reaper or another claim
    changes it. Terminal tasks and replaced leases are never reopened.
    """
    conn = _conn(db_config)
    try:
        conn.begin()
        with conn.cursor() as cursor:
            row = _locked_row(cursor, task_id)
            if row is None:
                raise MonitorTaskError("TASK_NOT_FOUND")
            if row["status"] not in {"LEASED", "RUNNING"}:
                raise MonitorTaskError("TASK_NOT_ACTIVE")
            if not lease_token or row["lease_device_id"] != device_id or row["lease_token"] != lease_token:
                raise MonitorTaskError("TASK_LEASE_STALE")
            now = _now()
            attempt = max(0, int(row.get("attempt") or 0) - 1)
            progress_value = _json_object(row.get("progress"))
            previous = progress_value.get("_scheduledRest")
            count = int(previous.get("count") or 0) if isinstance(previous, dict) else 0
            progress_value["_scheduledRest"] = {"at": now, "count": count + 1}
            serialized = json.dumps(progress_value, ensure_ascii=False)
            if row.get("_supplement"):
                stamp = supplements.unix_time(now)
                cursor.execute(
                    f"UPDATE {supplements.TABLE} SET status=0,attempt=%s,lease_device_id='',lease_token='',"
                    "lease_expires_at=0,available_at=%s,started_at=0,finished_at=0,"
                    "result_summary='{}',progress=%s,updated_at=%s WHERE local_task_id=%s",
                    (attempt, stamp, serialized, stamp, task_id),
                )
                summary = "{}"
            else:
                cursor.execute(
                    "UPDATE station_monitor_task SET status='PENDING',attempt=%s,lease_device_id='',lease_token='',"
                    "lease_expires_at=NULL,available_at=%s,started_at=NULL,finished_at=NULL,"
                    "result_summary=NULL,progress=%s,updated_at=%s WHERE local_task_id=%s",
                    (attempt, now, serialized, now, task_id),
                )
                summary = None
            row.update(status="PENDING", attempt=attempt, lease_device_id="", lease_token="",
                       lease_expires_at=None, available_at=now, started_at=None, finished_at=None,
                       result_summary=summary, progress=progress_value, updated_at=now)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    # Deliberately no round advancement, failure counter, or supplement enqueue.
    return task_payload(row)


def ack(db_config, task_id, device_id, lease_token):
    if task_id.startswith(supplements.PREFIX):
        return _supplement_action(db_config, task_id, device_id, lease_token, "ack")
    row = _get_row(db_config, task_id)
    _assert_lease(row, device_id, lease_token)
    now = _now()
    expires_at = (
        datetime.now(timezone.utc) + timedelta(seconds=LEASE_SECONDS)
    ).strftime("%Y-%m-%d %H:%M:%S")
    conn = _conn(db_config)
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                "UPDATE station_monitor_task SET status='RUNNING', lease_expires_at=%s, updated_at=%s "
                "WHERE local_task_id=%s AND lease_device_id=%s AND lease_token=%s",
                (expires_at, now, task_id, device_id, lease_token),
            )
        conn.commit()
    finally:
        conn.close()
    row["status"] = "RUNNING"
    row["lease_expires_at"] = expires_at
    return task_payload(row)


def progress(db_config, task_id, device_id, lease_token, progress_value):
    if task_id.startswith(supplements.PREFIX):
        return _supplement_action(db_config, task_id, device_id, lease_token, "progress", progress_value)
    row = _get_row(db_config, task_id)
    _assert_lease(row, device_id, lease_token)
    merged = _json_object(row.get("progress"))
    merged.update(progress_value or {})
    now = _now()
    expires_at = (
        datetime.now(timezone.utc) + timedelta(seconds=LEASE_SECONDS)
    ).strftime("%Y-%m-%d %H:%M:%S")
    conn = _conn(db_config)
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                "UPDATE station_monitor_task SET status='RUNNING', progress=%s, "
                "lease_expires_at=%s, updated_at=%s WHERE local_task_id=%s "
                "AND lease_device_id=%s AND lease_token=%s",
                (json.dumps(merged, ensure_ascii=False), expires_at, now, task_id, device_id, lease_token),
            )
        conn.commit()
    finally:
        conn.close()
    row["status"] = "RUNNING"
    row["lease_expires_at"] = expires_at
    row["progress"] = merged
    return task_payload(row)


def _advance_round(db_config, batch_id):
    _start_next_round(db_config, batch_id)


def _has_historical_data(db_config, source_key):
    """Only retry NO_MATCH for stations that have produced real data before."""
    if not source_key:
        return False
    conn = _conn(db_config)
    try:
        with conn.cursor() as cursor:
            checks = (
                "SELECT 1 FROM site_exploration_charging_station_result "
                "WHERE source_key=%s AND ("
                "(current_price IS NOT NULL AND current_price<>'') "
                "OR COALESCE(fast_total,0)>0 OR COALESCE(super_total,0)>0 "
                "OR COALESCE(slow_total,0)>0) LIMIT 1",
                "SELECT 1 FROM site_exploration_charging_station_dynamic_history "
                "WHERE source_key=%s AND ("
                "(current_price IS NOT NULL AND current_price<>'') "
                "OR COALESCE(pile_total,0)>0 OR COALESCE(fast_total,0)>0 "
                "OR COALESCE(super_total,0)>0 OR COALESCE(slow_total,0)>0) LIMIT 1",
            )
            for sql in checks:
                cursor.execute(sql, (source_key,))
                if cursor.fetchone():
                    return True
            return False
    finally:
        conn.close()


def _has_usable_amap_tariff(snapshot):
    """Accept actual timed price rows, never an empty sheet or a headline price."""
    price = _json_object(snapshot.get("price_json"))
    if price.get("priceSchedulePlatform") != "AMAP":
        return False
    for key in ("fastPrices", "slowPrices"):
        periods = price.get(key)
        if not isinstance(periods, list):
            continue
        for period in periods:
            if not isinstance(period, dict):
                continue
            match = re.fullmatch(r"(\d{1,2}):(\d{2})\s*[-–—~～至]\s*(\d{1,2}):(\d{2})",
                                 str(period.get("time") or "").replace("：", ":").strip())
            if not match:
                continue
            h1, m1, h2, m2 = map(int, match.groups())
            if not (0 <= h1 < 24 and 0 <= m1 < 60 and 0 <= h2 <= 24 and
                    0 <= m2 < 60 and (h2 < 24 or m2 == 0) and h1 * 60 + m1 != h2 * 60 + m2):
                continue
            try:
                value = float(period.get("totalPrice"))
            except (TypeError, ValueError, OverflowError):
                continue
            if math.isfinite(value) and value >= 0:
                return True
    return False


def _has_valid_task_result(cursor, row):
    """Use this task's own snapshot; Amap may supply tariff without pile status.

    Price-only acceptance is restricted to the actual Amap collecting scope,
    including Baidu-origin supplements assigned to Amap. It does not mean that
    live pile fields or a complete full-day tariff schedule were collected.
    """
    cursor.execute(
        "SELECT current_price,pile_total,pile_idle,pile_busy,pile_unknown,price_json "
        "FROM site_exploration_charging_station_dynamic_history "
        "WHERE task_id=%s AND source_key=%s",
        (row["local_task_id"], row["source_key"]),
    )
    has_amap_tariff = False
    for snapshot in cursor.fetchall():
        has_amap_tariff = has_amap_tariff or _has_usable_amap_tariff(snapshot)
        try:
            price = float(snapshot["current_price"])
            total = int(snapshot["pile_total"])
            idle = int(snapshot["pile_idle"])
            busy = int(snapshot["pile_busy"])
            unknown = int(snapshot["pile_unknown"])
        except (TypeError, ValueError, KeyError, OverflowError):
            continue
        if math.isfinite(price) and price >= 0 and total > 0 and 0 <= idle <= total and busy >= 0 and unknown == 0 and idle + busy == total:
            return True
    if has_amap_tariff:
        if row.get("_supplement"):
            return row.get("target_scope_key") == "AMAP_MIN240_647"
        cursor.execute("SELECT scope_key FROM station_monitor_batch WHERE id=%s", (row["batch_id"],))
        batch = cursor.fetchone()
        return bool(batch and batch.get("scope_key") == "AMAP_MIN240_647")
    return False


def _supports_supplement(cursor, row):
    cursor.execute("SELECT scope_key FROM station_monitor_batch WHERE id=%s", (row["batch_id"],))
    batch = cursor.fetchone()
    return bool(batch and batch.get("scope_key") in supplements.SCOPES)


def _missing_station_data(error_code, message):
    code = str(error_code or "").strip().upper()
    if code in {"NO_MATCH", "INCOMPLETE_STATION_CARD", "MISSING_STATION_DATA", "NO_STATION_DATA", "NO_VALID_DATA", "MISSING_PRICE", "MISSING_PILE_STATUS"}:
        return True
    if code != "COLLECTION_FAILED":
        return False
    return any(marker in str(message or "") for marker in (
        "详情首页数据不完整", "未采集到目标站点", "未获取到电价", "未读取到电价",
        "未获取到电桩", "未读取到电桩", "缺少实时电价", "缺少空闲/总枪数",
        "无有效电桩", "详情页无有效数据",
    ))


def _requeue_primary(cursor, row, now, summary, reason):
    retry_at = (_as_utc(now) + timedelta(seconds=10)).strftime("%Y-%m-%d %H:%M:%S")
    progress_value = _json_object(row.get("progress"))
    progress_value["_effectiveResultRetry"] = {"reason": reason, "at": now, "summary": summary}
    cursor.execute(
        "UPDATE station_monitor_task SET status='PENDING',lease_device_id='',lease_token='',"
        "lease_expires_at=NULL,available_at=%s,finished_at=NULL,result_summary=NULL,progress=%s,last_error=%s,updated_at=%s "
        "WHERE local_task_id=%s",
        (retry_at, json.dumps(progress_value, ensure_ascii=False), reason[:1000], now, row["local_task_id"]),
    )
    row.update(status="PENDING", lease_device_id="", lease_token="", lease_expires_at=None,
               available_at=retry_at, finished_at=None, result_summary=None, progress=progress_value)


def _finish_primary(cursor, row, now, status, summary, message="", supplement_reason=""):
    extra_id = supplements.enqueue(cursor, row, supplement_reason, now) if supplement_reason else None
    summary = dict(summary or {})
    if extra_id:
        summary["crossPlatformTaskId"] = extra_id
    cursor.execute(
        "UPDATE station_monitor_task SET status=%s,result_summary=%s,last_error=%s,lease_device_id='',"
        "lease_token='',lease_expires_at=NULL,finished_at=%s,updated_at=%s WHERE local_task_id=%s",
        (status, json.dumps(summary, ensure_ascii=False), message[:1000], now, now, row["local_task_id"]),
    )
    counter = "completed_count" if status == "COMPLETED" else "failed_count"
    cursor.execute(f"UPDATE station_monitor_round SET {counter}={counter}+1,updated_at=%s WHERE id=%s", (now, row["round_id"]))
    row.update(status=status, result_summary=summary, lease_device_id="", lease_token="",
               lease_expires_at=None, finished_at=now, last_error=message[:1000])


def _supplement_action(db_config, task_id, device_id, lease_token, action, value=None):
    conn = _conn(db_config)
    terminal = action in {"complete", "fail"}
    try:
        conn.begin()
        with conn.cursor() as cursor:
            row = _locked_row(cursor, task_id)
            _assert_lease(row, device_id, lease_token)
            now = _now()
            if terminal:
                valid = _has_valid_task_result(cursor, row)
                summary = value or {}
                supplements.finish(cursor, row, now, valid, summary, "" if valid else str(summary.get("error") or "NO_VALID_RESULT"))
            else:
                merged = None
                if action == "progress":
                    merged = _json_object(row.get("progress"))
                    merged.update(value or {})
                supplements.renew(cursor, row, now, merged)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    if terminal:
        _advance_round(db_config, int(row["batch_id"]))
    return task_payload(row)


def complete(db_config, task_id, device_id, lease_token, result_summary):
    if task_id.startswith(supplements.PREFIX):
        return _supplement_action(db_config, task_id, device_id, lease_token, "complete", result_summary)
    conn = _conn(db_config)
    try:
        conn.begin()
        with conn.cursor() as cursor:
            row = _locked_row(cursor, task_id)
            _assert_lease(row, device_id, lease_token)
            now = _now()
            summary = result_summary or {}
            skip_reason = str(summary.get("skipReason") or "").strip().upper()
            supported = _supports_supplement(cursor, row)
            valid = _has_valid_task_result(cursor, row) if supported else None
            can_retry = int(row.get("attempt") or 0) < int(row.get("max_attempts") or MAX_ATTEMPTS)
            # Fixed scopes retry missing effective data even if this station
            # never had historical data. Legacy modes keep their old policy.
            legacy_retry = not supported and skip_reason == "NO_MATCH" and not _json_object(row.get("progress")).get("_noMatchRetryScheduled") and _has_historical_data(db_config, row.get("source_key"))
            if can_retry and ((supported and not valid) or legacy_retry):
                reason = "NO_MATCH_RETRY" if skip_reason == "NO_MATCH" else "NO_VALID_RESULT_RETRY"
                _requeue_primary(cursor, row, now, summary, reason)
                if legacy_retry:
                    row["progress"]["_noMatchRetryScheduled"] = True
                    cursor.execute("UPDATE station_monitor_task SET progress=%s WHERE local_task_id=%s", (json.dumps(row["progress"], ensure_ascii=False), task_id))
            else:
                reason = ("NO_MATCH" if skip_reason == "NO_MATCH" else "NO_VALID_RESULT") if supported and not valid else ""
                _finish_primary(cursor, row, now, "COMPLETED", summary, supplement_reason=reason)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    if row["status"] != "PENDING":
        _advance_round(db_config, int(row["batch_id"]))
    return task_payload(row)


def fail(db_config, task_id, device_id, lease_token, error_code, error_message, retryable):
    message = f"{error_code or ''}: {error_message or ''}".strip(": ")[:1000]
    if task_id.startswith(supplements.PREFIX):
        return _supplement_action(db_config, task_id, device_id, lease_token, "fail", {"error": message})
    conn = _conn(db_config)
    try:
        conn.begin()
        with conn.cursor() as cursor:
            row = _locked_row(cursor, task_id)
            _assert_lease(row, device_id, lease_token)
            now = _now()
            supported = _supports_supplement(cursor, row)
            missing = _missing_station_data(error_code, error_message)
            valid = _has_valid_task_result(cursor, row) if supported else False
            can_retry = (bool(retryable) or (supported and missing)) and int(row.get("attempt") or 0) < int(row.get("max_attempts") or MAX_ATTEMPTS)
            if supported and valid:
                _finish_primary(cursor, row, now, "COMPLETED", {"effectiveResult": True, "reportedError": message})
            elif can_retry:
                _requeue_primary(cursor, row, now, {}, message)
            else:
                reason = ("NO_MATCH" if str(error_code).upper() == "NO_MATCH" else "MISSING_STATION_DATA") if supported and missing else ""
                _finish_primary(cursor, row, now, "FAILED", {}, message, reason)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    if row["status"] != "PENDING":
        _advance_round(db_config, int(row["batch_id"]))
    return task_payload(row)


def status(db_config):
    init_tables(db_config)
    conn = _conn(db_config)
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                "SELECT * FROM station_monitor_batch WHERE status='ACTIVE' ORDER BY id LIMIT 1"
            )
            batch = cursor.fetchone()
            if not batch:
                return {"active": False, "batch": None}
            cursor.execute(
                "SELECT status,COUNT(*) AS count FROM station_monitor_task WHERE batch_id=%s GROUP BY status",
                (batch["id"],),
            )
            counts = {row["status"]: int(row["count"]) for row in cursor.fetchall()}
            return {"active": True, "batch": batch, "taskStatusCounts": counts}
    finally:
        conn.close()


def _load_local_env() -> None:
    """Best-effort .env loader so the CLI behaves like the service."""
    for candidate in (".env", "../.env", "../../.env", "../../../.env"):
        path = os.path.abspath(candidate)
        if not os.path.isfile(path):
            continue
        with open(path, encoding="utf-8") as handle:
            for raw in handle:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                key = key.strip()
                value = value.strip().strip(chr(34)).strip(chr(39))
                if key and not os.getenv(key):
                    os.environ[key] = value
        return


def _db_config_from_env():
    from site_exploration_bridge import parse_mysql_database_url

    config = parse_mysql_database_url(os.getenv("EVCS_DATABASE_URL", ""))
    if not config:
        config = {
            "host": os.getenv("DB_HOST", ""),
            "port": _env_int("DB_PORT", 3306),
            "user": os.getenv("DB_USER", ""),
            "password": os.getenv("DB_PASSWORD", ""),
            "database": os.getenv("DB_NAME", ""),
        }
    config.setdefault("charset", os.getenv("DB_CHARSET", "utf8mb4"))
    config.setdefault("connect_timeout", _env_int("DB_CONNECT_TIMEOUT", 5))
    if not all(config.get(key) for key in ("host", "user", "database")):
        raise SystemExit("EVCS_DATABASE_URL(或 DB_* 变量)未配置，无法连接数据库")
    return config


def _main(argv=None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="重点站点小时监控批次工具")
    parser.add_argument("command", nargs="?", default="create-batch", choices=["create-batch"])
    parser.add_argument("--size", type=int, default=DEFAULT_BATCH_SIZE, help="批次站点数，0=全库不限制")
    parser.add_argument("--rounds", type=int, default=DEFAULT_TARGET_ROUNDS, help="目标轮次，0=无限循环")
    parser.add_argument("--interval", type=int, default=DEFAULT_INTERVAL_MINUTES, help="轮次间隔分钟，0=跑完立即下一轮")
    parser.add_argument("--all-stations", action="store_true", help="全库模式：装入结果表里的所有站点")
    parser.add_argument("--mode", default="DETAIL", choices=["DETAIL", "LIST"], help="采集模式")
    parser.add_argument("--city", default="", help="仅选择指定城市(复用批次时忽略)")
    parser.add_argument("--reuse-batches", default="", help="复用这些批次的站点, 例如 1,2,3,4")
    parser.add_argument("--close-active", action="store_true", help="先把当前 ACTIVE 批次标记为完成")
    args = parser.parse_args(argv)

    _load_local_env()
    db_config = _db_config_from_env()
    reuse_ids = [
        int(part)
        for part in args.reuse_batches.replace(",", ",").split(",")
        if part.strip()
    ]
    if args.close_active:
        closed = close_active_batches(db_config)
        print("closed_active_batches=%s" % closed)
    batch_id = create_batch(
        db_config,
        size=args.size,
        target_rounds=args.rounds,
        interval_minutes=args.interval,
        city=args.city,
        collection_mode=args.mode,
        reuse_batch_ids=reuse_ids,
        all_stations=True if args.all_stations else None,
    )
    print("created_batch_id=%s" % batch_id)
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
