import unittest
from datetime import datetime
from unittest.mock import MagicMock

from mysql_scan_runner import (
    MySQLScanRepository,
    execute_task,
    station_values,
    validate_database_target,
)


class FakeCursor:
    def __init__(self, task=None, existing_station=None):
        self.task = task
        self.existing_station = existing_station
        self.executed = []
        self.rowcount = 0
        self.lastrowid = 101
        self._result = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def execute(self, query, params=None):
        normalized = " ".join(query.split())
        self.executed.append((normalized, params))
        self.rowcount = 0
        self._result = None
        if normalized.startswith("SELECT * FROM scan_tasks"):
            self._result = dict(self.task) if self.task else None
        elif normalized.startswith("SELECT id FROM heavy_truck_stations"):
            self._result = self.existing_station
        elif normalized.startswith("UPDATE scan_tasks"):
            self.rowcount = 1
        elif normalized.startswith("INSERT INTO heavy_truck_stations"):
            self.lastrowid = 101
            self.rowcount = 1
        elif normalized.startswith("UPDATE heavy_truck_stations"):
            self.rowcount = 1
        elif normalized.startswith("INSERT INTO collection_logs"):
            self.rowcount = 1

    def fetchone(self):
        return self._result

    def fetchall(self):
        return []


class FakeConnection:
    def __init__(self, cursor):
        self.cursor_instance = cursor
        self.begun = False
        self.committed = False
        self.rolled_back = False
        self.closed = False

    def begin(self):
        self.begun = True

    def cursor(self):
        return self.cursor_instance

    def commit(self):
        self.committed = True

    def rollback(self):
        self.rolled_back = True

    def close(self):
        self.closed = True


class MySQLScanRunnerTests(unittest.TestCase):
    def test_database_guard_rejects_remote_and_non_test_targets(self):
        with self.assertRaisesRegex(RuntimeError, "只允许连接本机"):
            validate_database_target({"host": "203.0.113.10", "database": "evcs"})

        with self.assertRaisesRegex(RuntimeError, "名称含 test"):
            validate_database_target({"host": "127.0.0.1", "database": "evcs"})

        validate_database_target(
            {"host": "127.0.0.1", "database": "evcs_local_test"}
        )
        validate_database_target(
            {"host": "203.0.113.10", "database": "evcs"},
            allow_remote=True,
            allow_non_test_database=True,
        )

    def test_station_values_normalizes_json_city_and_datetime(self):
        values = station_values(
            {
                "station_name": "测试站",
                "search_city": "郑州",
                "address": "中原区测试路",
                "fast_prices": [{"time": "00:00-01:00", "total_price": "0.8"}],
                "collected_at": "2026-08-13T14:11:32+00:00",
            },
            {"city": "备用城市"},
        )

        self.assertEqual("郑州", values["city"])
        self.assertIn('"time":"00:00-01:00"', values["fast_prices"])
        self.assertEqual(datetime(2026, 8, 13, 14, 11, 32), values["collected_at"])

    def test_claim_task_uses_locking_read_and_marks_scanning(self):
        task = {
            "id": 7,
            "city": "郑州",
            "district": "中原区",
            "grid_index": 1,
        }
        cursor = FakeCursor(task=task)
        connection = FakeConnection(cursor)
        repository = MySQLScanRepository({}, connect_factory=lambda **kwargs: connection)

        claimed = repository.claim_task("device-1", task_id=7)

        self.assertEqual("scanning", claimed["status"])
        self.assertEqual("device-1", claimed["assigned_device"])
        self.assertTrue(connection.begun)
        self.assertTrue(connection.committed)
        self.assertTrue(connection.closed)
        self.assertIn("FOR UPDATE SKIP LOCKED", cursor.executed[0][0])
        self.assertIn("AND id = %s", cursor.executed[0][0])

    def test_complete_task_inserts_station_logs_and_done_status(self):
        cursor = FakeCursor()
        connection = FakeConnection(cursor)
        repository = MySQLScanRepository({}, connect_factory=lambda **kwargs: connection)
        task = {"id": 9, "city": "洛阳", "district": "新安县", "grid_index": 2}
        result = {
            "station_name": "测试重卡充电站",
            "address": "新安县测试路",
            "longitude": 112.06,
            "latitude": 34.72,
            "collected_at": "2026-08-13T14:11:32+00:00",
            "scan_task_id": 9,
        }

        station_ids = repository.complete_task(task, "device-1", [result])

        self.assertEqual([101], station_ids)
        self.assertTrue(connection.committed)
        statements = [query for query, _ in cursor.executed]
        self.assertTrue(any(query.startswith("INSERT INTO heavy_truck_stations") for query in statements))
        self.assertEqual(
            2,
            sum(query.startswith("INSERT INTO collection_logs") for query in statements),
        )
        self.assertTrue(
            any("SET status = 'done'" in query for query in statements)
        )

    def test_execute_task_marks_failure_when_crawler_raises(self):
        repository = MagicMock()
        task = {"id": 11}

        class BrokenCrawler:
            def __init__(self, **kwargs):
                pass

            def run_grid(self, current_task):
                raise RuntimeError("device failure")

        with self.assertRaisesRegex(RuntimeError, "device failure"):
            execute_task(
                repository,
                task,
                "device-1",
                crawler_factory=BrokenCrawler,
            )

        repository.fail_task.assert_called_once()
        repository.complete_task.assert_not_called()


if __name__ == "__main__":
    unittest.main()
