import csv
import tempfile
import time
import unittest
from pathlib import Path

from batch_runner import build_search_query, evaluate_detail, parse_adb_devices
from task_queue import StationTaskQueue


class StationTaskQueueTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "tasks.db"
        self.queue = StationTaskQueue(self.db_path)

    def tearDown(self):
        self.temp_dir.cleanup()

    def _write_csv(self):
        csv_path = Path(self.temp_dir.name) / "stations.csv"
        fieldnames = [
            "id", "name", "address", "latitude", "longitude", "sequence"
        ]
        rows = [
            {
                "id": "A",
                "name": "甲充电站",
                "address": "甲路1号",
                "latitude": "34.1",
                "longitude": "113.1",
                "sequence": "1",
            },
            {
                "id": "A",
                "name": "甲充电站",
                "address": "甲路1号",
                "latitude": "34.1",
                "longitude": "113.1",
                "sequence": "2",
            },
            {
                "id": "B",
                "name": "乙充电站",
                "address": "乙路2号",
                "latitude": "34.2",
                "longitude": "113.2",
                "sequence": "1",
            },
        ]
        with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)
        return csv_path

    def test_csv_import_groups_duplicate_station_ids(self):
        result = self.queue.import_csv(self._write_csv())

        self.assertEqual(3, result["source_rows"])
        self.assertEqual(2, result["unique_tasks"])
        self.assertEqual(2, self.queue.stats()["total"])
        self.assertEqual(2, len(self.queue.get_task("A")["payload"]["source_records"]))

    def test_user_task_preempts_normal_priority(self):
        self.queue.import_csv(self._write_csv())
        self.queue.enqueue_user_task(
            {"id": "USER", "name": "用户返回充电站"},
            priority=1000,
        )

        task = self.queue.claim("worker", "device")

        self.assertEqual("USER", task.station_id)

    def test_expired_lease_is_reclaimed_with_new_token(self):
        self.queue.import_csv(self._write_csv(), max_attempts=3)
        first = self.queue.claim("worker-a", "device-a", lease_seconds=0.01)
        time.sleep(0.03)
        second = self.queue.claim("worker-b", "device-b", lease_seconds=30)

        self.assertEqual(first.station_id, second.station_id)
        self.assertNotEqual(first.lease_token, second.lease_token)
        self.assertEqual(2, second.attempt_no)
        self.assertFalse(self.queue.complete(first, {}, 0, False))

    def test_user_request_during_lease_queues_rerun(self):
        self.queue.import_csv(self._write_csv())
        task = self.queue.claim("worker", "device")
        self.queue.enqueue_user_task(
            {"id": task.station_id, "name": "甲充电站"},
            priority=1000,
        )

        self.assertTrue(
            self.queue.complete(task, {"station_name": "甲充电站"}, 50, False)
        )
        queued = self.queue.get_task(task.station_id)
        self.assertEqual("pending", queued["status"])
        self.assertEqual(0, queued["attempts"])
        self.assertEqual(1000, queued["priority"])

    def test_release_refunds_attempt(self):
        self.queue.import_csv(self._write_csv())
        task = self.queue.claim("worker", "device")

        self.assertTrue(self.queue.release(task, refund_attempt=True))
        released = self.queue.get_task(task.station_id)
        self.assertEqual("pending", released["status"])
        self.assertEqual(0, released["attempts"])


class BatchRunnerTests(unittest.TestCase):
    def test_parse_adb_devices_ignores_offline_devices(self):
        output = """List of devices attached
SERIAL1\tdevice product:p model:m device:d transport_id:1
SERIAL2\toffline transport_id:2
"""
        self.assertEqual(
            [{"serial": "SERIAL1", "metadata": {"product": "p", "model": "m", "device": "d", "transport_id": "1"}}],
            parse_adb_devices(output),
        )

    def test_detail_evaluation_requires_operational_fields(self):
        assessment = evaluate_detail(
            {
                "detail_verified": True,
                "station_name": "测试充电站",
                "address": "测试路1号",
                "business_hours": "24小时",
                "operator": "测试运营商",
            }
        )
        self.assertTrue(assessment["detailed"])
        self.assertGreaterEqual(assessment["score"], 70)

    def test_supercharge_name_counts_as_charging_station(self):
        assessment = evaluate_detail(
            {
                "detail_verified": True,
                "station_name": "铁门锦阳重卡超充站",
                "address": "铁门镇",
                "business_hours": "暂无营业时间",
                "facilities": ["地上"],
            }
        )
        self.assertTrue(assessment["detailed"])

    def test_generic_station_search_includes_address(self):
        query = build_search_query(
            {"name": "重卡充电站", "address": "铁门镇经一路"}
        )
        self.assertIn("铁门镇经一路", query)


if __name__ == "__main__":
    unittest.main()
