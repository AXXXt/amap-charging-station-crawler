import csv
import hashlib
import json
import sqlite3
import time
import uuid
from collections import defaultdict
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


TASK_STATUSES = ("pending", "leased", "succeeded", "failed")


def utc_now_iso():
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class ClaimedTask:
    task_id: int
    station_id: str
    payload: dict
    priority: int
    attempt_no: int
    max_attempts: int
    lease_token: str
    attempt_id: int


class StationTaskQueue:
    def __init__(self, db_path):
        self.db_path = str(Path(db_path).resolve())
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self.init_schema()

    @contextmanager
    def _connect(self):
        connection = sqlite3.connect(self.db_path, timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 30000")
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            yield connection
        finally:
            connection.close()

    def init_schema(self):
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS station_tasks (
                    task_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    station_id TEXT NOT NULL UNIQUE,
                    payload_json TEXT NOT NULL,
                    source_sequence INTEGER NOT NULL DEFAULT 0,
                    priority INTEGER NOT NULL DEFAULT 100,
                    status TEXT NOT NULL DEFAULT 'pending',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    max_attempts INTEGER NOT NULL DEFAULT 3,
                    available_at REAL NOT NULL DEFAULT 0,
                    lease_owner TEXT,
                    lease_token TEXT,
                    lease_expires_at REAL,
                    rerun_requested INTEGER NOT NULL DEFAULT 0,
                    result_json TEXT,
                    completeness_score REAL NOT NULL DEFAULT 0,
                    detailed INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    CHECK (status IN ('pending', 'leased', 'succeeded', 'failed'))
                );
                CREATE INDEX IF NOT EXISTS idx_station_tasks_claim
                ON station_tasks(status, available_at, priority DESC, source_sequence, task_id);

                CREATE TABLE IF NOT EXISTS task_attempts (
                    attempt_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id INTEGER NOT NULL,
                    station_id TEXT NOT NULL,
                    attempt_no INTEGER NOT NULL,
                    device_serial TEXT NOT NULL,
                    lease_token TEXT NOT NULL,
                    strategy TEXT NOT NULL DEFAULT 'poi_id',
                    outcome TEXT NOT NULL DEFAULT 'running',
                    error TEXT,
                    result_json TEXT,
                    started_at TEXT NOT NULL,
                    finished_at TEXT,
                    FOREIGN KEY(task_id) REFERENCES station_tasks(task_id)
                );
                CREATE INDEX IF NOT EXISTS idx_task_attempts_task
                ON task_attempts(task_id, attempt_no);

                CREATE TABLE IF NOT EXISTS device_workers (
                    device_serial TEXT PRIMARY KEY,
                    worker_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    current_task_id INTEGER,
                    metadata_json TEXT,
                    last_heartbeat REAL NOT NULL,
                    updated_at TEXT NOT NULL
                );
                """
            )

    @staticmethod
    def _station_id(payload, prefix="ROW"):
        station_id = (payload.get("id") or "").strip()
        if station_id:
            return station_id
        digest_source = "|".join(
            [
                payload.get("name", ""),
                str(payload.get("latitude", "")),
                str(payload.get("longitude", "")),
            ]
        )
        return prefix + "-" + hashlib.sha1(digest_source.encode("utf-8")).hexdigest()[:16]

    def import_csv(self, csv_path, base_priority=100, max_attempts=3):
        with Path(csv_path).open(encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.DictReader(handle))

        grouped = defaultdict(list)
        for row in rows:
            station_id = self._station_id(row)
            row["id"] = station_id
            grouped[station_id].append(row)

        now_iso = utc_now_iso()
        inserted = 0
        updated = 0
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                for station_id, source_records in grouped.items():
                    payload = dict(source_records[0])
                    payload["source_records"] = source_records
                    payload["duplicate_count"] = len(source_records)
                    sequences = []
                    for record in source_records:
                        try:
                            sequences.append(int(record.get("sequence") or 0))
                        except ValueError:
                            pass
                    source_sequence = min(sequences) if sequences else 0
                    payload_json = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
                    existing = connection.execute(
                        "SELECT task_id FROM station_tasks WHERE station_id = ?",
                        (station_id,),
                    ).fetchone()
                    if existing:
                        connection.execute(
                            """
                            UPDATE station_tasks
                            SET payload_json = ?, source_sequence = ?,
                                priority = CASE WHEN priority > ? THEN priority ELSE ? END,
                                max_attempts = CASE WHEN max_attempts > ? THEN max_attempts ELSE ? END,
                                updated_at = ?
                            WHERE station_id = ?
                            """,
                            (
                                payload_json, source_sequence,
                                base_priority, base_priority,
                                max_attempts, max_attempts,
                                now_iso, station_id,
                            ),
                        )
                        updated += 1
                    else:
                        connection.execute(
                            """
                            INSERT INTO station_tasks (
                                station_id, payload_json, source_sequence, priority,
                                status, attempts, max_attempts, available_at,
                                created_at, updated_at
                            ) VALUES (?, ?, ?, ?, 'pending', 0, ?, 0, ?, ?)
                            """,
                            (
                                station_id, payload_json, source_sequence,
                                base_priority, max_attempts, now_iso, now_iso,
                            ),
                        )
                        inserted += 1
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise

        return {
            "source_rows": len(rows),
            "unique_tasks": len(grouped),
            "inserted": inserted,
            "updated": updated,
        }

    def enqueue_user_task(self, payload, priority=1000, max_attempts=3):
        payload = dict(payload)
        station_id = self._station_id(payload, prefix="USER")
        payload["id"] = station_id
        payload["request_source"] = "user"
        payload_json = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        now_iso = utc_now_iso()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    "SELECT status FROM station_tasks WHERE station_id = ?",
                    (station_id,),
                ).fetchone()
                if row is None:
                    connection.execute(
                        """
                        INSERT INTO station_tasks (
                            station_id, payload_json, source_sequence, priority,
                            status, attempts, max_attempts, available_at,
                            created_at, updated_at
                        ) VALUES (?, ?, 0, ?, 'pending', 0, ?, 0, ?, ?)
                        """,
                        (station_id, payload_json, priority, max_attempts, now_iso, now_iso),
                    )
                elif row["status"] == "leased":
                    connection.execute(
                        """
                        UPDATE station_tasks
                        SET payload_json = ?, priority = ?, max_attempts = ?,
                            rerun_requested = 1, updated_at = ?
                        WHERE station_id = ?
                        """,
                        (payload_json, priority, max_attempts, now_iso, station_id),
                    )
                else:
                    connection.execute(
                        """
                        UPDATE station_tasks
                        SET payload_json = ?, priority = ?, status = 'pending',
                            attempts = 0, max_attempts = ?, available_at = 0,
                            lease_owner = NULL, lease_token = NULL,
                            lease_expires_at = NULL, rerun_requested = 0,
                            last_error = NULL, updated_at = ?
                        WHERE station_id = ?
                        """,
                        (payload_json, priority, max_attempts, now_iso, station_id),
                    )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return station_id

    def _requeue_expired_leases(self, connection, now_epoch):
        now_iso = utc_now_iso()
        connection.execute(
            """
            UPDATE station_tasks
            SET status = 'pending', available_at = ?, lease_owner = NULL,
                lease_token = NULL, lease_expires_at = NULL,
                last_error = 'lease expired', updated_at = ?
            WHERE status = 'leased' AND lease_expires_at < ?
              AND attempts < max_attempts
            """,
            (now_epoch, now_iso, now_epoch),
        )
        connection.execute(
            """
            UPDATE station_tasks
            SET status = 'failed', lease_owner = NULL, lease_token = NULL,
                lease_expires_at = NULL, last_error = 'lease expired', updated_at = ?
            WHERE status = 'leased' AND lease_expires_at < ?
              AND attempts >= max_attempts
            """,
            (now_iso, now_epoch),
        )

    def claim(self, worker_id, device_serial, lease_seconds=180):
        now_epoch = time.time()
        now_iso = utc_now_iso()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                self._requeue_expired_leases(connection, now_epoch)
                row = connection.execute(
                    """
                    SELECT * FROM station_tasks
                    WHERE status = 'pending'
                      AND available_at <= ?
                      AND attempts < max_attempts
                    ORDER BY priority DESC, source_sequence ASC, task_id ASC
                    LIMIT 1
                    """,
                    (now_epoch,),
                ).fetchone()
                if row is None:
                    connection.execute("COMMIT")
                    return None

                lease_token = uuid.uuid4().hex
                attempt_no = row["attempts"] + 1
                connection.execute(
                    """
                    UPDATE station_tasks
                    SET status = 'leased', attempts = ?, lease_owner = ?,
                        lease_token = ?, lease_expires_at = ?, updated_at = ?
                    WHERE task_id = ? AND status = 'pending'
                    """,
                    (
                        attempt_no, worker_id, lease_token,
                        now_epoch + lease_seconds, now_iso, row["task_id"],
                    ),
                )
                attempt_cursor = connection.execute(
                    """
                    INSERT INTO task_attempts (
                        task_id, station_id, attempt_no, device_serial,
                        lease_token, started_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        row["task_id"], row["station_id"], attempt_no,
                        device_serial, lease_token, now_iso,
                    ),
                )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise

        return ClaimedTask(
            task_id=row["task_id"],
            station_id=row["station_id"],
            payload=json.loads(row["payload_json"]),
            priority=row["priority"],
            attempt_no=attempt_no,
            max_attempts=row["max_attempts"],
            lease_token=lease_token,
            attempt_id=attempt_cursor.lastrowid,
        )

    def heartbeat(self, task, worker_id, device_serial, lease_seconds=180):
        now_epoch = time.time()
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE station_tasks
                SET lease_expires_at = ?, updated_at = ?
                WHERE task_id = ? AND status = 'leased'
                  AND lease_owner = ? AND lease_token = ?
                """,
                (
                    now_epoch + lease_seconds, utc_now_iso(), task.task_id,
                    worker_id, task.lease_token,
                ),
            )
        self.heartbeat_device(device_serial, worker_id, "busy", task.task_id)
        return cursor.rowcount == 1

    def set_attempt_strategy(self, task, strategy):
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE task_attempts
                SET strategy = ?
                WHERE attempt_id = ? AND lease_token = ?
                """,
                (strategy, task.attempt_id, task.lease_token),
            )
        return cursor.rowcount == 1

    def complete(self, task, result, completeness_score, detailed):
        result_json = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
        now_iso = utc_now_iso()
        now_epoch = time.time()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    """
                    SELECT status, lease_token, rerun_requested
                    FROM station_tasks WHERE task_id = ?
                    """,
                    (task.task_id,),
                ).fetchone()
                if (
                    row is None
                    or row["status"] != "leased"
                    or row["lease_token"] != task.lease_token
                ):
                    connection.execute("ROLLBACK")
                    return False

                if row["rerun_requested"]:
                    connection.execute(
                        """
                        UPDATE station_tasks
                        SET status = 'pending', attempts = 0, available_at = ?,
                            lease_owner = NULL, lease_token = NULL,
                            lease_expires_at = NULL, rerun_requested = 0,
                            result_json = ?, completeness_score = ?, detailed = ?,
                            last_error = NULL, updated_at = ?
                        WHERE task_id = ?
                        """,
                        (
                            now_epoch, result_json, completeness_score,
                            int(detailed), now_iso, task.task_id,
                        ),
                    )
                    outcome = "succeeded_rerun_queued"
                else:
                    connection.execute(
                        """
                        UPDATE station_tasks
                        SET status = 'succeeded', lease_owner = NULL,
                            lease_token = NULL, lease_expires_at = NULL,
                            result_json = ?, completeness_score = ?, detailed = ?,
                            last_error = NULL, updated_at = ?
                        WHERE task_id = ?
                        """,
                        (
                            result_json, completeness_score, int(detailed),
                            now_iso, task.task_id,
                        ),
                    )
                    outcome = "succeeded"

                connection.execute(
                    """
                    UPDATE task_attempts
                    SET outcome = ?, result_json = ?, finished_at = ?
                    WHERE attempt_id = ? AND lease_token = ?
                    """,
                    (outcome, result_json, now_iso, task.attempt_id, task.lease_token),
                )
                connection.execute("COMMIT")
                return True
            except Exception:
                connection.execute("ROLLBACK")
                raise

    def fail(
        self,
        task,
        error,
        retryable=True,
        retry_delay=5,
        result=None,
        completeness_score=0,
    ):
        now_iso = utc_now_iso()
        now_epoch = time.time()
        result_json = (
            json.dumps(result, ensure_ascii=False, separators=(",", ":"))
            if result is not None
            else None
        )
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    """
                    SELECT status, lease_token, attempts, max_attempts, rerun_requested
                    FROM station_tasks WHERE task_id = ?
                    """,
                    (task.task_id,),
                ).fetchone()
                if (
                    row is None
                    or row["status"] != "leased"
                    or row["lease_token"] != task.lease_token
                ):
                    connection.execute("ROLLBACK")
                    return False

                should_retry = retryable and (
                    row["attempts"] < row["max_attempts"]
                    or bool(row["rerun_requested"])
                )
                if should_retry:
                    status = "pending"
                    available_at = now_epoch + retry_delay
                    attempts = 0 if row["rerun_requested"] else row["attempts"]
                    rerun_requested = 0
                    outcome = "retry"
                else:
                    status = "failed"
                    available_at = now_epoch
                    attempts = row["attempts"]
                    rerun_requested = row["rerun_requested"]
                    outcome = "failed"

                connection.execute(
                    """
                    UPDATE station_tasks
                    SET status = ?, attempts = ?, available_at = ?,
                        lease_owner = NULL, lease_token = NULL,
                        lease_expires_at = NULL, rerun_requested = ?,
                        result_json = COALESCE(?, result_json),
                        completeness_score = CASE
                            WHEN ? > completeness_score THEN ?
                            ELSE completeness_score
                        END,
                        last_error = ?, updated_at = ?
                    WHERE task_id = ?
                    """,
                    (
                        status, attempts, available_at, rerun_requested,
                        result_json, completeness_score, completeness_score,
                        str(error)[:1000], now_iso, task.task_id,
                    ),
                )
                connection.execute(
                    """
                    UPDATE task_attempts
                    SET outcome = ?, error = ?, result_json = ?, finished_at = ?
                    WHERE attempt_id = ? AND lease_token = ?
                    """,
                    (
                        outcome, str(error)[:1000], result_json, now_iso,
                        task.attempt_id, task.lease_token,
                    ),
                )
                connection.execute("COMMIT")
                return True
            except Exception:
                connection.execute("ROLLBACK")
                raise

    def release(self, task, reason="worker interrupted", refund_attempt=True):
        now_iso = utc_now_iso()
        now_epoch = time.time()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    "SELECT status, lease_token, attempts FROM station_tasks WHERE task_id = ?",
                    (task.task_id,),
                ).fetchone()
                if (
                    row is None
                    or row["status"] != "leased"
                    or row["lease_token"] != task.lease_token
                ):
                    connection.execute("ROLLBACK")
                    return False
                attempts = max(0, row["attempts"] - 1) if refund_attempt else row["attempts"]
                connection.execute(
                    """
                    UPDATE station_tasks
                    SET status = 'pending', attempts = ?, available_at = ?,
                        lease_owner = NULL, lease_token = NULL,
                        lease_expires_at = NULL, last_error = ?, updated_at = ?
                    WHERE task_id = ?
                    """,
                    (attempts, now_epoch, str(reason)[:1000], now_iso, task.task_id),
                )
                connection.execute(
                    """
                    UPDATE task_attempts
                    SET outcome = 'interrupted', error = ?, finished_at = ?
                    WHERE attempt_id = ? AND lease_token = ?
                    """,
                    (str(reason)[:1000], now_iso, task.attempt_id, task.lease_token),
                )
                connection.execute("COMMIT")
                return True
            except Exception:
                connection.execute("ROLLBACK")
                raise

    def register_device(self, device_serial, worker_id, metadata=None):
        metadata_json = json.dumps(metadata or {}, ensure_ascii=False)
        now_epoch = time.time()
        now_iso = utc_now_iso()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO device_workers (
                    device_serial, worker_id, status, current_task_id,
                    metadata_json, last_heartbeat, updated_at
                ) VALUES (?, ?, 'idle', NULL, ?, ?, ?)
                ON CONFLICT(device_serial) DO UPDATE SET
                    worker_id = excluded.worker_id,
                    status = 'idle',
                    current_task_id = NULL,
                    metadata_json = excluded.metadata_json,
                    last_heartbeat = excluded.last_heartbeat,
                    updated_at = excluded.updated_at
                """,
                (device_serial, worker_id, metadata_json, now_epoch, now_iso),
            )

    def heartbeat_device(self, device_serial, worker_id, status, current_task_id=None):
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE device_workers
                SET worker_id = ?, status = ?, current_task_id = ?,
                    last_heartbeat = ?, updated_at = ?
                WHERE device_serial = ?
                """,
                (
                    worker_id, status, current_task_id,
                    time.time(), utc_now_iso(), device_serial,
                ),
            )

    def mark_device_offline(self, device_serial, worker_id):
        self.heartbeat_device(device_serial, worker_id, "offline", None)

    def requeue_failed(self, station_ids=None):
        station_ids = [station_id for station_id in (station_ids or []) if station_id]
        now_iso = utc_now_iso()
        with self._connect() as connection:
            if station_ids:
                placeholders = ",".join("?" for _ in station_ids)
                cursor = connection.execute(
                    f"""
                    UPDATE station_tasks
                    SET status = 'pending', attempts = 0, available_at = 0,
                        lease_owner = NULL, lease_token = NULL,
                        lease_expires_at = NULL, last_error = NULL, updated_at = ?
                    WHERE status = 'failed' AND station_id IN ({placeholders})
                    """,
                    (now_iso, *station_ids),
                )
            else:
                cursor = connection.execute(
                    """
                    UPDATE station_tasks
                    SET status = 'pending', attempts = 0, available_at = 0,
                        lease_owner = NULL, lease_token = NULL,
                        lease_expires_at = NULL, last_error = NULL, updated_at = ?
                    WHERE status = 'failed'
                    """,
                    (now_iso,),
                )
        return cursor.rowcount

    def recover_orphaned_leases(self, force=False):
        now_epoch = time.time()
        now_iso = utc_now_iso()
        with self._connect() as connection:
            if force:
                cursor = connection.execute(
                    """
                    UPDATE station_tasks
                    SET status = 'pending', attempts = CASE
                            WHEN attempts > 0 THEN attempts - 1 ELSE 0 END,
                        available_at = 0, lease_owner = NULL, lease_token = NULL,
                        lease_expires_at = NULL, last_error = 'orphaned lease recovered',
                        updated_at = ?
                    WHERE status = 'leased'
                    """,
                    (now_iso,),
                )
            else:
                cursor = connection.execute(
                    """
                    UPDATE station_tasks
                    SET status = 'pending', attempts = CASE
                            WHEN attempts > 0 THEN attempts - 1 ELSE 0 END,
                        available_at = 0, lease_owner = NULL, lease_token = NULL,
                        lease_expires_at = NULL, last_error = 'expired lease recovered',
                        updated_at = ?
                    WHERE status = 'leased' AND lease_expires_at < ?
                    """,
                    (now_iso, now_epoch),
                )
        return cursor.rowcount

    def stats(self):
        with self._connect() as connection:
            status_rows = connection.execute(
                "SELECT status, COUNT(*) AS count FROM station_tasks GROUP BY status"
            ).fetchall()
            totals = {status: 0 for status in TASK_STATUSES}
            totals.update({row["status"]: row["count"] for row in status_rows})
            summary = connection.execute(
                """
                SELECT COUNT(*) AS total,
                       SUM(detailed) AS detailed,
                       AVG(CASE WHEN status = 'succeeded' THEN completeness_score END) AS avg_score
                FROM station_tasks
                """
            ).fetchone()
        total = summary["total"] or 0
        detailed = summary["detailed"] or 0
        return {
            **totals,
            "total": total,
            "detailed": detailed,
            "detailed_rate": detailed / total if total else 0,
            "average_score": summary["avg_score"] or 0,
        }

    def iter_task_rows(self):
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT task_id, station_id, payload_json, status, attempts,
                       max_attempts, priority, result_json,
                       completeness_score, detailed, last_error,
                       created_at, updated_at
                FROM station_tasks
                ORDER BY source_sequence ASC, task_id ASC
                """
            ).fetchall()
        for row in rows:
            item = dict(row)
            item["payload"] = json.loads(item.pop("payload_json"))
            result_json = item.pop("result_json")
            item["result"] = json.loads(result_json) if result_json else None
            item["detailed"] = bool(item["detailed"])
            yield item

    def get_task(self, station_id):
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM station_tasks WHERE station_id = ?",
                (station_id,),
            ).fetchone()
        if row is None:
            return None
        item = dict(row)
        item["payload"] = json.loads(item.pop("payload_json"))
        result_json = item.pop("result_json")
        item["result"] = json.loads(result_json) if result_json else None
        item["detailed"] = bool(item["detailed"])
        return item
