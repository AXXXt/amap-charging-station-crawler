import argparse
import json
import os
import threading
from datetime import datetime, timezone

import pymysql

from batch_runner import discover_devices
from crawler import ADB_PATH, AmapCrawler


LOCAL_DB_HOSTS = {"127.0.0.1", "localhost", "::1"}
STATION_COLUMNS = (
    "station_name",
    "operator",
    "address",
    "city",
    "business_hours",
    "current_price",
    "parking_fee",
    "occupancy_fee",
    "longitude",
    "latitude",
    "fast_available",
    "fast_total",
    "fast_power",
    "super_available",
    "super_total",
    "super_power",
    "slow_available",
    "slow_total",
    "slow_power",
    "fast_prices",
    "slow_prices",
    "facilities",
    "tags",
    "favorite_count",
    "collected_at",
)
TEXT_LIMITS = {
    "station_name": 255,
    "operator": 100,
    "address": 500,
    "city": 50,
    "business_hours": 100,
    "current_price": 20,
    "parking_fee": 200,
    "occupancy_fee": 300,
    "fast_available": 10,
    "fast_total": 10,
    "fast_power": 50,
    "super_available": 10,
    "super_total": 10,
    "super_power": 50,
    "slow_available": 10,
    "slow_total": 10,
    "slow_power": 50,
    "favorite_count": 10,
}


def load_db_config():
    config = {
        "host": os.getenv("DB_HOST", ""),
        "port": int(os.getenv("DB_PORT", "3306")),
        "user": os.getenv("DB_USER", ""),
        "password": os.getenv("DB_PASSWORD", ""),
        "database": os.getenv("DB_NAME", ""),
        "charset": os.getenv("DB_CHARSET", "utf8mb4"),
        "connect_timeout": int(os.getenv("DB_CONNECT_TIMEOUT", "5")),
    }
    missing = [name for name in ("host", "user", "database") if not config[name]]
    if missing:
        raise RuntimeError("缺少数据库环境变量: " + ", ".join(missing))
    return config


def validate_database_target(
    config,
    allow_remote=False,
    allow_non_test_database=False,
):
    host = str(config.get("host") or "").strip().lower()
    database = str(config.get("database") or "").strip().lower()
    if host not in LOCAL_DB_HOSTS and not allow_remote:
        raise RuntimeError(
            "安全保护：默认只允许连接本机 MySQL；远程库需显式传入 --allow-remote"
        )
    is_test_database = (
        database == "test"
        or database.startswith("test_")
        or database.endswith("_test")
    )
    if not is_test_database and not allow_non_test_database:
        raise RuntimeError(
            "安全保护：默认只允许名称含 test 的数据库；"
            "非测试库需显式传入 --allow-non-test-database"
        )


def normalize_collected_at(value):
    if not value:
        return datetime.now(timezone.utc).replace(tzinfo=None)
    if isinstance(value, datetime):
        parsed = value
    else:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def json_field(value):
    if value in (None, "", [], {}):
        return None
    if isinstance(value, str):
        try:
            json.loads(value)
            return value
        except json.JSONDecodeError:
            return json.dumps(value, ensure_ascii=False)
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def compact_log_data(payload, max_bytes=60000):
    serialized = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        default=str,
    )
    size = len(serialized.encode("utf-8"))
    if size <= max_bytes:
        return serialized
    summary = {
        "truncated": True,
        "original_bytes": size,
        "scan_task_id": payload.get("scan_task_id"),
        "station_name": payload.get("station_name", ""),
        "address": payload.get("address", ""),
    }
    return json.dumps(summary, ensure_ascii=False, separators=(",", ":"))


def limited_text(field, value):
    text = str(value or "")
    limit = TEXT_LIMITS.get(field)
    return text[:limit] if limit else text


def text_for_mysql(value, max_bytes=60000):
    encoded = str(value or "").encode("utf-8")
    if len(encoded) <= max_bytes:
        return encoded.decode("utf-8")
    return encoded[:max_bytes].decode("utf-8", errors="ignore")


def station_values(result, task):
    values = {
        "station_name": result.get("station_name", ""),
        "operator": result.get("operator", ""),
        "address": result.get("address", ""),
        "city": result.get("search_city") or result.get("city") or task.get("city", ""),
        "business_hours": result.get("business_hours", ""),
        "current_price": result.get("current_price", ""),
        "parking_fee": result.get("parking_fee", ""),
        "occupancy_fee": result.get("occupancy_fee", ""),
        "longitude": result.get("longitude"),
        "latitude": result.get("latitude"),
        "fast_available": result.get("fast_available", ""),
        "fast_total": result.get("fast_total", ""),
        "fast_power": result.get("fast_power", ""),
        "super_available": result.get("super_available", ""),
        "super_total": result.get("super_total", ""),
        "super_power": result.get("super_power", ""),
        "slow_available": result.get("slow_available", ""),
        "slow_total": result.get("slow_total", ""),
        "slow_power": result.get("slow_power", ""),
        "fast_prices": json_field(result.get("fast_prices")),
        "slow_prices": json_field(result.get("slow_prices")),
        "facilities": json_field(result.get("facilities")),
        "tags": json_field(result.get("tags")),
        "favorite_count": result.get("favorite_count", ""),
        "collected_at": normalize_collected_at(result.get("collected_at")),
    }
    for field in TEXT_LIMITS:
        values[field] = limited_text(field, values.get(field))
    return values


class MySQLScanRepository:
    def __init__(self, config, connect_factory=pymysql.connect):
        self.config = dict(config)
        self.connect_factory = connect_factory

    def _connect(self):
        return self.connect_factory(
            **self.config,
            autocommit=False,
            cursorclass=pymysql.cursors.DictCursor,
        )

    def claim_task(self, device_serial, task_id=None):
        connection = self._connect()
        try:
            connection.begin()
            with connection.cursor() as cursor:
                query = "SELECT * FROM scan_tasks WHERE status = 'pending'"
                params = []
                if task_id is not None:
                    query += " AND id = %s"
                    params.append(task_id)
                query += " ORDER BY id LIMIT 1 FOR UPDATE SKIP LOCKED"
                cursor.execute(query, params)
                task = cursor.fetchone()
                if task is None:
                    connection.rollback()
                    return None

                cursor.execute(
                    """
                    UPDATE scan_tasks
                    SET status = 'scanning', assigned_device = %s,
                        station_count = 0, started_at = CURRENT_TIMESTAMP,
                        completed_at = NULL
                    WHERE id = %s AND status = 'pending'
                    """,
                    (device_serial, task["id"]),
                )
                if cursor.rowcount != 1:
                    connection.rollback()
                    return None
            connection.commit()
            task = dict(task)
            task["status"] = "scanning"
            task["assigned_device"] = device_serial
            return task
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def get_task(self, task_id):
        connection = self._connect()
        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT * FROM scan_tasks WHERE id = %s", (task_id,))
                row = cursor.fetchone()
                return dict(row) if row else None
        finally:
            connection.close()

    def stats(self):
        connection = self._connect()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT status, COUNT(*) AS count FROM scan_tasks GROUP BY status"
                )
                rows = cursor.fetchall()
        finally:
            connection.close()
        result = {"pending": 0, "scanning": 0, "done": 0, "failed": 0}
        result.update({row["status"]: row["count"] for row in rows})
        result["total"] = sum(result.values())
        return result

    def reset_task(self, task_id):
        connection = self._connect()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE scan_tasks
                    SET status = 'pending', assigned_device = NULL,
                        station_count = 0, started_at = NULL, completed_at = NULL
                    WHERE id = %s
                    """,
                    (task_id,),
                )
                changed = cursor.rowcount
            connection.commit()
            return changed == 1
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def release_task(self, task_id, device_serial):
        connection = self._connect()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE scan_tasks
                    SET status = 'pending', assigned_device = NULL,
                        station_count = 0, started_at = NULL, completed_at = NULL
                    WHERE id = %s AND status = 'scanning' AND assigned_device = %s
                    """,
                    (task_id, device_serial),
                )
                changed = cursor.rowcount
            connection.commit()
            return changed == 1
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def fail_task(self, task, device_serial, error):
        connection = self._connect()
        try:
            connection.begin()
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO collection_logs
                        (station_id, layer, raw_data, success, error_msg)
                    VALUES (NULL, 'accessibility', %s, 0, %s)
                    """,
                    (compact_log_data(dict(task)), text_for_mysql(error)),
                )
                cursor.execute(
                    """
                    UPDATE scan_tasks
                    SET status = 'failed', station_count = 0,
                        completed_at = CURRENT_TIMESTAMP
                    WHERE id = %s AND status = 'scanning' AND assigned_device = %s
                    """,
                    (task["id"], device_serial),
                )
                if cursor.rowcount != 1:
                    raise RuntimeError("任务状态已变化，拒绝写入失败状态")
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def complete_task(self, task, device_serial, results):
        connection = self._connect()
        station_ids = []
        try:
            connection.begin()
            with connection.cursor() as cursor:
                for result in results:
                    station_id = self._upsert_station(cursor, task, result)
                    station_ids.append(station_id)
                    cursor.execute(
                        """
                        INSERT INTO collection_logs
                            (station_id, layer, raw_data, success, error_msg)
                        VALUES (%s, 'accessibility', %s, 1, NULL)
                        """,
                        (station_id, compact_log_data(result)),
                    )

                task_summary = {
                    "scan_task_id": task["id"],
                    "city": task.get("city", ""),
                    "district": task.get("district", ""),
                    "grid_index": task.get("grid_index"),
                    "assigned_device": device_serial,
                    "station_count": len(station_ids),
                    "station_ids": station_ids,
                }
                cursor.execute(
                    """
                    INSERT INTO collection_logs
                        (station_id, layer, raw_data, success, error_msg)
                    VALUES (NULL, 'accessibility', %s, 1, NULL)
                    """,
                    (compact_log_data(task_summary),),
                )

                cursor.execute(
                    """
                    UPDATE scan_tasks
                    SET status = 'done', station_count = %s,
                        completed_at = CURRENT_TIMESTAMP
                    WHERE id = %s AND status = 'scanning' AND assigned_device = %s
                    """,
                    (len(station_ids), task["id"], device_serial),
                )
                if cursor.rowcount != 1:
                    raise RuntimeError("任务状态已变化，拒绝写入完成状态")
            connection.commit()
            return station_ids
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _upsert_station(cursor, task, result):
        values = station_values(result, task)
        address = values["address"]
        if address:
            cursor.execute(
                """
                SELECT id FROM heavy_truck_stations
                WHERE station_name = %s AND city = %s AND address = %s
                ORDER BY id DESC LIMIT 1 FOR UPDATE
                """,
                (values["station_name"], values["city"], address),
            )
        else:
            cursor.execute(
                """
                SELECT id FROM heavy_truck_stations
                WHERE station_name = %s AND city = %s
                ORDER BY id DESC LIMIT 1 FOR UPDATE
                """,
                (values["station_name"], values["city"]),
            )
        existing = cursor.fetchone()
        ordered_values = [values[name] for name in STATION_COLUMNS]
        if existing:
            assignments = ", ".join(
                f"{name} = %s" for name in STATION_COLUMNS if name != "station_name"
            )
            update_values = [
                values[name] for name in STATION_COLUMNS if name != "station_name"
            ]
            cursor.execute(
                f"UPDATE heavy_truck_stations SET {assignments} WHERE id = %s",
                update_values + [existing["id"]],
            )
            return existing["id"]

        columns = ", ".join(STATION_COLUMNS)
        placeholders = ", ".join(["%s"] * len(STATION_COLUMNS))
        cursor.execute(
            f"INSERT INTO heavy_truck_stations ({columns}) VALUES ({placeholders})",
            ordered_values,
        )
        return cursor.lastrowid


def choose_device(requested, adb_path=ADB_PATH):
    devices = discover_devices(adb_path)
    if requested:
        devices = [item for item in devices if item["serial"] == requested]
    if not devices:
        raise RuntimeError("没有找到指定的在线 ADB 设备")
    if len(devices) > 1:
        raise RuntimeError("检测到多台设备，请通过 --device 明确指定一台")
    return devices[0]["serial"]


def execute_task(
    repository,
    task,
    device_serial,
    visual=False,
    crawler_factory=AmapCrawler,
    adb_path=ADB_PATH,
):
    stop_event = threading.Event()
    checker = None
    if visual:
        from visual_check import create_qianwen_checker

        checker = create_qianwen_checker(device_serial)
    try:
        crawler = crawler_factory(
            serial=device_serial,
            visual_checker=checker,
            stop_event=stop_event,
            adb_path=adb_path,
        )
        results = crawler.run_grid(task)
        station_ids = repository.complete_task(task, device_serial, results)
        return {
            "task_id": task["id"],
            "status": "done",
            "station_count": len(station_ids),
            "station_ids": station_ids,
        }
    except KeyboardInterrupt:
        stop_event.set()
        repository.release_task(task["id"], device_serial)
        raise
    except Exception as error:
        repository.fail_task(task, device_serial, error)
        raise


def run_command(args, repository):
    if args.task_id is None and not args.allow_any_pending:
        raise RuntimeError(
            "安全保护：请使用 --task-id 指定测试任务，"
            "或显式传入 --allow-any-pending"
        )
    device_serial = choose_device(args.device, args.adb_path)
    completed = []
    for index in range(args.max_tasks):
        task_id = args.task_id if index == 0 else None
        task = repository.claim_task(device_serial, task_id=task_id)
        if task is None:
            break
        completed.append(
            execute_task(
                repository,
                task,
                device_serial,
                visual=args.visual,
                adb_path=args.adb_path,
            )
        )
        if args.task_id is not None:
            break
    print(json.dumps({"completed": completed}, ensure_ascii=False, indent=2))
    return 0


def build_parser():
    parser = argparse.ArgumentParser(description="MySQL 网格普查任务本地联调工具")
    parser.add_argument("--allow-remote", action="store_true")
    parser.add_argument("--allow-non-test-database", action="store_true")
    subparsers = parser.add_subparsers(dest="command", required=True)

    status_parser = subparsers.add_parser("status", help="查看 scan_tasks 状态统计")
    status_parser.set_defaults(action="status")

    show_parser = subparsers.add_parser("show", help="查看单个网格任务")
    show_parser.add_argument("--task-id", type=int, required=True)
    show_parser.set_defaults(action="show")

    reset_parser = subparsers.add_parser("reset", help="把本地测试任务重置为 pending")
    reset_parser.add_argument("--task-id", type=int, required=True)
    reset_parser.set_defaults(action="reset")

    run_parser = subparsers.add_parser("run", help="领取并执行网格任务")
    run_parser.add_argument("--task-id", type=int)
    run_parser.add_argument("--allow-any-pending", action="store_true")
    run_parser.add_argument("--device", default=os.getenv("DEVICE_SERIAL") or None)
    run_parser.add_argument("--adb-path", default=ADB_PATH)
    run_parser.add_argument("--max-tasks", type=int, default=1)
    run_parser.add_argument("--visual", action="store_true")
    run_parser.set_defaults(action="run")
    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()
    config = load_db_config()
    validate_database_target(
        config,
        allow_remote=args.allow_remote,
        allow_non_test_database=args.allow_non_test_database,
    )
    repository = MySQLScanRepository(config)

    if args.action == "status":
        print(json.dumps(repository.stats(), ensure_ascii=False, indent=2))
        return 0
    if args.action == "show":
        print(json.dumps(repository.get_task(args.task_id), ensure_ascii=False, indent=2, default=str))
        return 0
    if args.action == "reset":
        changed = repository.reset_task(args.task_id)
        print(json.dumps({"task_id": args.task_id, "reset": changed}, ensure_ascii=False))
        return 0
    return run_command(args, repository)


if __name__ == "__main__":
    raise SystemExit(main())
