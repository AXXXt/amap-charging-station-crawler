"""Rolling hourly monitor scheduler for selected charging stations.

This module is intentionally isolated from the normal Henan task table. It
creates its own batches, rounds and tasks, so the existing full-site pipeline
keeps its original behavior.
"""
from __future__ import annotations

import json
import secrets
import uuid
from datetime import datetime, timedelta, timezone

import pymysql


TASK_TYPE = "HENAN_POI_DETAIL"
DEFAULT_BATCH_SIZE = 100
DEFAULT_TARGET_ROUNDS = 24
DEFAULT_INTERVAL_MINUTES = 60
LEASE_SECONDS = 900
MAX_ATTEMPTS = 3


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
            type VARCHAR(32) COLLATE utf8mb4_general_ci NOT NULL DEFAULT 'HENAN_POI_DETAIL',
            status VARCHAR(16) COLLATE utf8mb4_general_ci NOT NULL DEFAULT 'PENDING',
            attempt SMALLINT UNSIGNED NOT NULL DEFAULT 0,
            max_attempts SMALLINT UNSIGNED NOT NULL DEFAULT 3,
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
        conn.commit()
    finally:
        conn.close()


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
        "maxRecoveryAttempts": 2,
        "sourceSiteId": "",
        "sourceSiteOrder": 0,
        "stationId": row.get("amap_poi_id") or "",
        "stationName": row.get("station_name") or "",
        "stationAddress": row.get("station_address") or "",
        "stationLatitude": latitude,
        "stationLongitude": longitude,
        "stationSequence": int(row.get("sequence_no") or 0),
        "sourcePayload": source_payload,
    }


def legacy_task(row):
    """Return a row shaped like the original authoritative Henan task row."""
    payload = task_payload(row)
    return {
        "id": row["local_task_id"],
        "type": TASK_TYPE,
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
    }


def get_task(db_config, task_id):
    conn = _conn(db_config)
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                "SELECT * FROM station_monitor_task WHERE local_task_id=%s LIMIT 1",
                (task_id,),
            )
            row = cursor.fetchone()
        return legacy_task(row) if row else None
    finally:
        conn.close()


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


def _active_batch_id(db_config):
    conn = _conn(db_config)
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                "SELECT id FROM station_monitor_batch WHERE status='ACTIVE' ORDER BY id LIMIT 1"
            )
            row = cursor.fetchone()
        return int(row["id"]) if row else None
    finally:
        conn.close()


def _candidate_rows(conn, size, city):
    city_filter = "AND r.city=%s" if city else ""
    args = ([city] if city else []) + [size]
    if city:
        query = f"""
            SELECT r.source_key, r.source_station_id, r.matched_station_name,
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
                SELECT r.source_key, r.source_station_id, r.matched_station_name,
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
            SELECT source_key, source_station_id, matched_station_name,
                   city, district, station_address, latitude, longitude
              FROM ranked
             WHERE rn <= 8
             ORDER BY city, rn
             LIMIT %s
        """
    with conn.cursor() as cursor:
        cursor.execute(query, args)
        return cursor.fetchall()


def create_batch(db_config, size=DEFAULT_BATCH_SIZE, target_rounds=DEFAULT_TARGET_ROUNDS,
                 interval_minutes=DEFAULT_INTERVAL_MINUTES, city=""):
    init_tables(db_config)
    now = _now()
    code = "MON-" + datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    conn = _conn(db_config)
    try:
        candidates = _candidate_rows(conn, size, city.strip())
        if not candidates:
            raise RuntimeError("NO_ELIGIBLE_MONITOR_STATIONS")
        with conn.cursor() as cursor:
            cursor.execute(
                "INSERT INTO station_monitor_batch "
                "(batch_code,name,status,station_count,target_rounds,target_interval_minutes,created_at,updated_at) "
                "VALUES (%s,%s,'ACTIVE',%s,%s,%s,%s,%s)",
                (
                    code,
                    f"重点站点监控批次 {code}",
                    len(candidates),
                    target_rounds,
                    interval_minutes,
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


def _ensure_active_batch(db_config):
    lock_conn = _conn(db_config)
    try:
        with lock_conn.cursor() as cursor:
            cursor.execute("SELECT GET_LOCK('station_monitor_active_batch', 30) AS got")
            got = cursor.fetchone()
            if not got or int(got.get("got") or 0) != 1:
                raise RuntimeError("MONITOR_BATCH_LOCK_TIMEOUT")
        batch_id = _active_batch_id(db_config)
        if batch_id:
            return batch_id
        return create_batch(db_config)
    finally:
        try:
            with lock_conn.cursor() as cursor:
                cursor.execute("SELECT RELEASE_LOCK('station_monitor_active_batch')")
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
                    cursor.execute(
                        "UPDATE station_monitor_round SET status='COMPLETED', finished_at=%s, updated_at=%s "
                        "WHERE id=%s AND status<>'COMPLETED'",
                        (now, now, current_round_row["id"]),
                    )
                if current_round and current_round_row and current_round_row.get("started_at"):
                    interval_minutes = int(batch.get("target_interval_minutes") or DEFAULT_INTERVAL_MINUTES)
                    target_at = _as_utc(current_round_row["started_at"]) + timedelta(minutes=interval_minutes)
                    if target_at > _as_utc(now):
                        next_available_at = target_at.strftime("%Y-%m-%d %H:%M:%S")
                if current_round >= int(batch["target_rounds"] or DEFAULT_TARGET_ROUNDS):
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
                "priority,type,status,attempt,max_attempts,available_at,created_at,updated_at) "
                "SELECT SHA2(CONCAT(%s,':',%s,':',bs.source_key),256), UUID(), bs.batch_id, %s, %s, "
                "bs.source_key, bs.amap_poi_id, bs.station_name, bs.station_address, bs.city, bs.district, "
                "bs.latitude, bs.longitude, bs.sequence_no, 80, 'HENAN_POI_DETAIL', 'PENDING', 0, 3, %s, %s, %s "
                "FROM station_monitor_batch_station bs WHERE bs.batch_id=%s",
                (batch_id, next_round, round_id, next_round, next_available_at, now, now, batch_id),
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


def claim(db_config, device):
    init_tables(db_config)
    _reap_expired(db_config)
    try:
        batch_id = _ensure_active_batch(db_config)
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
                    "SELECT * FROM station_monitor_task "
                    "WHERE batch_id=%s AND round_no=%s AND status='PENDING' "
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


def _get_row(db_config, task_id):
    conn = _conn(db_config)
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                "SELECT * FROM station_monitor_task WHERE local_task_id=%s LIMIT 1",
                (task_id,),
            )
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


def ack(db_config, task_id, device_id, lease_token):
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


def complete(db_config, task_id, device_id, lease_token, result_summary):
    row = _get_row(db_config, task_id)
    _assert_lease(row, device_id, lease_token)
    now = _now()
    conn = _conn(db_config)
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                "UPDATE station_monitor_task SET status='COMPLETED', result_summary=%s, "
                "lease_device_id='', lease_token='', lease_expires_at=NULL, finished_at=%s, updated_at=%s "
                "WHERE local_task_id=%s AND lease_device_id=%s AND lease_token=%s",
                (json.dumps(result_summary or {}, ensure_ascii=False), now, now, task_id, device_id, lease_token),
            )
            cursor.execute(
                "UPDATE station_monitor_round SET completed_count=completed_count+1, updated_at=%s WHERE id=%s",
                (now, row["round_id"]),
            )
        conn.commit()
    finally:
        conn.close()
    _advance_round(db_config, int(row["batch_id"]))
    row["status"] = "COMPLETED"
    row["lease_device_id"] = ""
    row["lease_token"] = ""
    row["lease_expires_at"] = None
    return task_payload(row)


def fail(db_config, task_id, device_id, lease_token, error_code, error_message, retryable):
    row = _get_row(db_config, task_id)
    _assert_lease(row, device_id, lease_token)
    now = _now()
    can_retry = bool(retryable) and int(row.get("attempt") or 0) < int(row.get("max_attempts") or MAX_ATTEMPTS)
    status = "PENDING" if can_retry else "FAILED"
    available_at = (
        datetime.now(timezone.utc) + timedelta(seconds=10)
    ).strftime("%Y-%m-%d %H:%M:%S") if can_retry else now
    message = f"{error_code or ''}: {error_message or ''}".strip(": ")[:1000]
    conn = _conn(db_config)
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                "UPDATE station_monitor_task SET status=%s, lease_device_id='', lease_token='', "
                "lease_expires_at=NULL, available_at=%s, last_error=%s, "
                "finished_at=CASE WHEN %s='FAILED' THEN %s ELSE NULL END, updated_at=%s "
                "WHERE local_task_id=%s AND lease_device_id=%s AND lease_token=%s",
                (status, available_at, message, status, now, now, task_id, device_id, lease_token),
            )
            if status == "FAILED":
                cursor.execute(
                    "UPDATE station_monitor_round SET failed_count=failed_count+1, updated_at=%s WHERE id=%s",
                    (now, row["round_id"]),
                )
        conn.commit()
    finally:
        conn.close()
    if status == "FAILED":
        _advance_round(db_config, int(row["batch_id"]))
    row["status"] = status
    row["lease_device_id"] = ""
    row["lease_token"] = ""
    row["lease_expires_at"] = None
    row["last_error"] = message
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
