"""One-shot cross-platform monitor tasks, linked to their original round.

Formal batch membership is never changed. A separate task id also keeps the
mobile outbox's task/station deduplication independent of the original attempt.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

TABLE = "station_monitor_supplement_task"
PREFIX = "XPL-"
SCOPES = {
    "BAIDU_MIN240_499": "AMAP_MIN240_647",
    "AMAP_MIN240_647": "BAIDU_MIN240_499",
}
PENDING, LEASED, RUNNING, COMPLETED, FAILED = range(5)
STATUS_NAMES = ("PENDING", "LEASED", "RUNNING", "COMPLETED", "FAILED")
LEASE_SECONDS = 900

CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS station_monitor_supplement_task (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '主键',
    local_task_id VARCHAR(64) NOT NULL DEFAULT '' COMMENT '跨平台补采任务编号',
    origin_task_id VARCHAR(64) NOT NULL DEFAULT '' COMMENT '原平台任务编号，每个原任务最多补采一次',
    origin_batch_id BIGINT UNSIGNED NOT NULL DEFAULT 0 COMMENT '原平台正式批次编号',
    origin_round_id BIGINT UNSIGNED NOT NULL DEFAULT 0 COMMENT '原平台轮次编号',
    target_scope_key VARCHAR(64) NOT NULL DEFAULT '' COMMENT '临时补采目标平台范围',
    reason VARCHAR(64) NOT NULL DEFAULT '' COMMENT '触发补采的原因',
    status TINYINT UNSIGNED NOT NULL DEFAULT 0 COMMENT '状态：0待领取 1已领取 2运行中 3完成 4失败',
    attempt SMALLINT UNSIGNED NOT NULL DEFAULT 0 COMMENT '已领取次数，上限一次',
    max_attempts SMALLINT UNSIGNED NOT NULL DEFAULT 1 COMMENT '跨平台领取次数上限，固定一次',
    lease_device_id VARCHAR(64) NOT NULL DEFAULT '' COMMENT '当前领取设备编号',
    lease_token VARCHAR(64) NOT NULL DEFAULT '' COMMENT '当前租约令牌',
    lease_expires_at INT UNSIGNED NOT NULL DEFAULT 0 COMMENT '租约到期时间（Unix 时间戳）',
    available_at INT UNSIGNED NOT NULL DEFAULT 0 COMMENT '可领取时间（Unix 时间戳）',
    started_at INT UNSIGNED NOT NULL DEFAULT 0 COMMENT '补采开始时间（Unix 时间戳）',
    finished_at INT UNSIGNED NOT NULL DEFAULT 0 COMMENT '补采结束时间（Unix 时间戳）',
    last_error VARCHAR(1000) NOT NULL DEFAULT '' COMMENT '最近错误信息',
    progress JSON NOT NULL DEFAULT (JSON_OBJECT()) COMMENT '补采进度',
    result_summary JSON NOT NULL DEFAULT (JSON_OBJECT()) COMMENT '补采结果摘要',
    created_at INT UNSIGNED NOT NULL DEFAULT 0 COMMENT '创建时间（Unix 时间戳）',
    updated_at INT UNSIGNED NOT NULL DEFAULT 0 COMMENT '更新时间（Unix 时间戳）',
    PRIMARY KEY (id),
    UNIQUE KEY uk_supplement_local_task (local_task_id),
    UNIQUE KEY uk_supplement_origin_task (origin_task_id),
    KEY idx_supplement_target_status (target_scope_key, status, available_at),
    KEY idx_supplement_round_status (origin_round_id, status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci COMMENT='固定站点每轮一次的跨平台补采任务'
"""

IDENTITY_COLUMNS = (
    "t.batch_id,t.round_id,t.round_no,t.source_key,t.amap_poi_id,t.station_name,"
    "t.station_address,t.city,t.district,t.latitude,t.longitude,t.sequence_no,"
    "t.priority,t.collection_mode,t.type"
)


def unix_time(value):
    if isinstance(value, (int, float)):
        return int(value)
    parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.timestamp())


def utc_text(value):
    return datetime.fromtimestamp(int(value), timezone.utc).strftime("%Y-%m-%d %H:%M:%S") if value else None


def decode(row):
    if not row:
        return None
    row = dict(row)
    row["status"] = STATUS_NAMES[int(row["status"])]
    for field in ("lease_expires_at", "available_at", "started_at", "finished_at", "created_at", "updated_at"):
        row[field] = utc_text(row.get(field))
    row["_supplement"] = True
    return row


def metadata(row):
    return {
        "originTaskId": row["origin_task_id"],
        "originBatchId": int(row["origin_batch_id"]),
        "originRoundId": int(row["origin_round_id"]),
        "targetScopeKey": row["target_scope_key"],
        "reason": row["reason"],
        "maxAttempts": 1,
    }


def get_row(cursor, task_id, lock=False, skip_locked=False):
    # Lock only the supplement. Locking the joined source batch/task here can
    # deadlock against round advancement, which locks the batch first.
    suffix = " FOR UPDATE" + (" SKIP LOCKED" if skip_locked else "") if lock else ""
    cursor.execute(f"SELECT * FROM {TABLE} WHERE local_task_id=%s LIMIT 1" + suffix, (task_id,))
    row = cursor.fetchone()
    if row is None:
        return None
    cursor.execute(f"SELECT {IDENTITY_COLUMNS} FROM station_monitor_task t WHERE t.local_task_id=%s", (row["origin_task_id"],))
    identity = cursor.fetchone()
    return decode({**row, **identity}) if identity else None


def enqueue(cursor, origin, reason, now):
    """Use the caller's transaction so terminal handling and enqueue are atomic."""
    if origin.get("_supplement"):
        return None
    cursor.execute("SELECT scope_key,status FROM station_monitor_batch WHERE id=%s", (origin["batch_id"],))
    batch = cursor.fetchone()
    target = SCOPES.get(batch.get("scope_key")) if batch and batch.get("status") == "ACTIVE" else None
    if not target:
        return None
    stamp = unix_time(now)
    task_id = PREFIX + str(uuid.uuid5(uuid.NAMESPACE_URL, "monitor-supplement:" + origin["local_task_id"]))
    cursor.execute(
        f"INSERT IGNORE INTO {TABLE} "
        "(local_task_id,origin_task_id,origin_batch_id,origin_round_id,target_scope_key,reason,"
        "available_at,created_at,updated_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
        (task_id, origin["local_task_id"], origin["batch_id"], origin["round_id"], target,
         reason, stamp, stamp, stamp),
    )
    return task_id


def expire(cursor, now, *, round_id=None, scope_key=None):
    """Never requeue an expired supplement: its single lease was already used."""
    stamp = unix_time(now)
    # Keep expiration on the indexed active slice rather than scanning all
    # completed supplements on every mobile poll.
    filter_sql, filter_params = "", ()
    if round_id is not None:
        filter_sql, filter_params = " AND origin_round_id=%s", (round_id,)
    elif scope_key is not None:
        filter_sql, filter_params = " AND target_scope_key=%s", (scope_key,)
    cursor.execute(
        f"UPDATE {TABLE} SET status=4,lease_device_id='',lease_token='',lease_expires_at=0,"
        "finished_at=%s,updated_at=%s,last_error='LEASE_EXPIRED' "
        "WHERE status IN (1,2) AND lease_expires_at<=%s" + filter_sql,
        (stamp, stamp, stamp) + filter_params,
    )


def has_active(cursor, round_id, now):
    expire(cursor, now, round_id=round_id)
    cursor.execute(f"SELECT COUNT(*) AS count FROM {TABLE} WHERE origin_round_id=%s AND status IN (0,1,2)", (round_id,))
    return int(cursor.fetchone()["count"]) > 0


def claim(cursor, device, scope_key, now, token):
    if scope_key not in SCOPES:
        return None
    stamp = unix_time(now)
    expire(cursor, now, scope_key=scope_key)
    cursor.execute(
        f"SELECT s.local_task_id FROM {TABLE} s "
        "JOIN station_monitor_task t ON t.local_task_id=s.origin_task_id "
        "JOIN station_monitor_batch b ON b.id=s.origin_batch_id "
        "JOIN station_monitor_round r ON r.id=s.origin_round_id "
        "WHERE s.target_scope_key=%s AND s.status=0 AND s.attempt=0 AND s.available_at<=%s "
        "AND b.status='ACTIVE' AND b.current_round=t.round_no "
        "AND r.status='RUNNING' ORDER BY s.available_at,s.id LIMIT 20",
        (scope_key, stamp),
    )
    row = None
    for candidate in cursor.fetchall():
        item = get_row(cursor, candidate["local_task_id"], lock=True, skip_locked=True)
        if item and item["status"] == "PENDING" and int(item["attempt"]) == 0:
            row = item
            break
    if row is None:
        return None
    cursor.execute(
        f"UPDATE {TABLE} SET status=1,attempt=1,lease_device_id=%s,lease_token=%s,"
        "lease_expires_at=%s,started_at=%s,updated_at=%s WHERE id=%s AND status=0 AND attempt=0",
        (device["id"], token, stamp + LEASE_SECONDS, stamp, stamp, row["id"]),
    )
    row.update(status="LEASED", attempt=1, lease_device_id=device["id"], lease_token=token,
               lease_expires_at=utc_text(stamp + LEASE_SECONDS), started_at=utc_text(stamp))
    return row


def renew(cursor, row, now, progress=None):
    stamp = unix_time(now)
    if progress is None:
        cursor.execute(
            f"UPDATE {TABLE} SET status=2,lease_expires_at=%s,updated_at=%s WHERE id=%s",
            (stamp + LEASE_SECONDS, stamp, row["id"]),
        )
    else:
        cursor.execute(
            f"UPDATE {TABLE} SET status=2,progress=%s,lease_expires_at=%s,updated_at=%s WHERE id=%s",
            (json.dumps(progress, ensure_ascii=False), stamp + LEASE_SECONDS, stamp, row["id"]),
        )
        row["progress"] = progress
    row.update(status="RUNNING", lease_expires_at=utc_text(stamp + LEASE_SECONDS))


def finish(cursor, row, now, valid, summary, error=""):
    stamp = unix_time(now)
    cursor.execute(
        f"UPDATE {TABLE} SET status=%s,result_summary=%s,last_error=%s,lease_device_id='',"
        "lease_token='',lease_expires_at=0,finished_at=%s,updated_at=%s WHERE id=%s",
        (COMPLETED if valid else FAILED, json.dumps(summary or {}, ensure_ascii=False), error[:1000], stamp, stamp, row["id"]),
    )
    row.update(status="COMPLETED" if valid else "FAILED", result_summary=summary or {}, last_error=error[:1000],
               lease_device_id="", lease_token="", lease_expires_at=None, finished_at=utc_text(stamp))
