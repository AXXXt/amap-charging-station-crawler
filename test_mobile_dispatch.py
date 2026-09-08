import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

import api_server


class MobileDispatchTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.temp_dir.name) / "mobile.db")
        self.patchers = [
            patch.object(api_server, "MOBILE_DB_PATH", self.db_path),
            patch.object(api_server, "MOBILE_ACTIVATION_CODE", "activate"),
            patch.object(api_server, "MOBILE_ADMIN_API_KEY", "admin"),
            patch.object(api_server.site_exploration_bridge, "enabled", False),
            patch.object(api_server, "LEGACY_MYSQL_SYNC_ENABLED", False),
        ]
        for item in self.patchers:
            item.start()
        api_server.init_mobile_db()
        self.devices = {}

    def tearDown(self):
        for item in reversed(self.patchers):
            item.stop()
        self.temp_dir.cleanup()

    def register(self, code):
        response = api_server.mobile_register(
            api_server.MobileRegisterRequest(
                activationCode="activate",
                deviceCode=code,
                name=code,
            )
        )
        token = response["deviceToken"]
        self.devices[code] = token
        return token

    def add_tasks(self, count):
        conn = api_server.mobile_conn()
        now = api_server._mobile_utc()
        for index in range(count):
            conn.execute(
                """INSERT INTO scan_task (
                       id, type, priority, province, city, district, keyword,
                       search_region, status, attempt, max_attempts,
                       available_at, created_at
                   ) VALUES (?, 'SITE_STATION_DETAIL', 1000, '', '', '', ?, '',
                             'PENDING', 0, 3, ?, ?)""",
                (f"task-{index:03d}", f"站点-{index}", now, now),
            )
        conn.commit()
        conn.close()

    def add_duplicate_station_tasks(self):
        conn = api_server.mobile_conn()
        now = api_server._mobile_utc()
        for index, source_order in enumerate((1, 2)):
            conn.execute(
                """INSERT INTO scan_task (
                       id, type, priority, province, city, district, keyword,
                       search_region, status, attempt, max_attempts,
                       available_at, created_at, source_site_id, source_site_order,
                       source_station_id, source_sequence
                   ) VALUES (?, 'SITE_STATION_DETAIL', 1000, '', '', '', ?, '',
                             'PENDING', 0, 3, ?, ?, ?, ?, ?, ?)""",
                (
                    f"duplicate-task-{index}",
                    f"同一站点-{index}",
                    now,
                    now,
                    f"source-site-{index}",
                    source_order,
                    "same-physical-station",
                    1,
                ),
            )
        conn.commit()
        conn.close()

    def claim(self, code):
        token = self.devices[code]
        return api_server.mobile_claim_task(
            api_server.MobileClaimRequest(deviceCode=code),
            authorization=f"Bearer {token}",
        )["task"]

    def complete(self, code, task):
        token = self.devices[code]
        return api_server.mobile_complete_task(
            task["id"],
            api_server.MobileTaskActionRequest(
                deviceCode=code,
                leaseToken=task["leaseToken"],
                resultSummary={"stations": 1},
            ),
            authorization=f"Bearer {token}",
        )

    def test_duplicate_source_station_is_claimed_only_once(self):
        self.add_duplicate_station_tasks()
        self.register("device-a")
        self.register("device-b")

        first = self.claim("device-a")
        second = self.claim("device-b")

        self.assertEqual("duplicate-task-0", first["id"])
        self.assertIsNone(second)
        self.complete("device-a", first)
        self.assertIsNone(self.claim("device-b"))

    def test_four_devices_dynamically_finish_without_duplicate_claims(self):
        self.add_tasks(100)
        for index in range(4):
            self.register(f"device-{index}")

        def worker(code):
            claimed = []
            while True:
                task = self.claim(code)
                if task is None:
                    return claimed
                claimed.append(task["id"])
                self.complete(code, task)

        with ThreadPoolExecutor(max_workers=4) as executor:
            batches = list(executor.map(worker, self.devices))
        all_ids = [task_id for batch in batches for task_id in batch]
        self.assertEqual(100, len(all_ids))
        self.assertEqual(100, len(set(all_ids)))
        self.assertEqual(100, all_ids.__len__())

        conn = api_server.mobile_conn()
        statuses = conn.execute(
            "SELECT status, COUNT(*) AS count FROM scan_task GROUP BY status"
        ).fetchall()
        conn.close()
        self.assertEqual({"COMPLETED": 100}, {row["status"]: row["count"] for row in statuses})

    def test_expired_lease_is_reclaimed_and_old_worker_cannot_complete(self):
        self.add_tasks(1)
        first_token = self.register("device-a")
        self.register("device-b")
        with patch.object(api_server, "MOBILE_LEASE_SECONDS", 0.01):
            first = self.claim("device-a")
            time.sleep(0.03)
            second = self.claim("device-b")
        self.assertEqual(first["id"], second["id"])
        self.assertNotEqual(first["leaseToken"], second["leaseToken"])
        with self.assertRaises(api_server.HTTPException) as raised:
            api_server.mobile_complete_task(
                first["id"],
                api_server.MobileTaskActionRequest(
                    deviceCode="device-a", leaseToken=first["leaseToken"]
                ),
                authorization=f"Bearer {first_token}",
            )
        self.assertEqual(409, raised.exception.status_code)
        self.assertEqual("TASK_LEASE_STALE", raised.exception.detail)

    def test_replacement_device_duplicate_keeps_first_observation(self):
        self.add_tasks(1)
        self.register("device-a")
        self.register("device-b")
        with patch.object(api_server, "MOBILE_LEASE_SECONDS", 60):
            first = self.claim("device-a")
            accepted = api_server.mobile_upload_observations(
                api_server.MobileObservationUploadRequest(
                    deviceCode="device-a",
                    leaseToken=first["leaseToken"],
                    observations=[
                        {
                            "observationId": "station:first",
                            "stationId": "station-1",
                            "taskId": first["id"],
                            "payload": {"stationName": "首个设备结果"},
                        }
                    ],
                ),
                authorization=f"Bearer {self.devices['device-a']}",
            )
        self.assertEqual(["station:first"], accepted["acceptedIds"])

        conn = api_server.mobile_conn()
        conn.execute(
            "UPDATE scan_task SET lease_expires_at = ? WHERE id = ?",
            ("2000-01-01T00:00:00+00:00", first["id"]),
        )
        conn.commit()
        conn.close()
        with patch.object(api_server, "MOBILE_LEASE_SECONDS", 60):
            second = self.claim("device-b")
            duplicate = api_server.mobile_upload_observations(
                api_server.MobileObservationUploadRequest(
                    deviceCode="device-b",
                    leaseToken=second["leaseToken"],
                    observations=[
                        {
                            "observationId": "station:replacement",
                            "stationId": "station-1",
                            "taskId": second["id"],
                            "payload": {"stationName": "接替设备结果"},
                        }
                    ],
                ),
                authorization=f"Bearer {self.devices['device-b']}",
            )
        self.assertEqual([], duplicate["acceptedIds"])
        self.assertEqual(["station:replacement"], duplicate["duplicateIds"])

        conn = api_server.mobile_conn()
        rows = conn.execute(
            "SELECT observation_id, device_id, payload FROM station_observation"
        ).fetchall()
        first_device = conn.execute(
            "SELECT id FROM collector_device WHERE device_code = ?",
            ("device-a",),
        ).fetchone()
        conn.close()
        self.assertEqual(1, len(rows))
        self.assertEqual("station:first", rows[0]["observation_id"])
        # The original row remains owned by the first device.
        self.assertEqual(first_device["id"], rows[0]["device_id"])
        self.assertIn("首个设备结果", rows[0]["payload"])

    def test_stale_worker_upload_is_rejected_but_duplicate_is_idempotent(self):
        self.add_tasks(1)
        first_token = self.register("device-a")
        self.register("device-b")
        with patch.object(api_server, "MOBILE_LEASE_SECONDS", 0.01):
            first = self.claim("device-a")
        time.sleep(0.03)
        with patch.object(api_server, "MOBILE_LEASE_SECONDS", 60):
            second = self.claim("device-b")
            stale = api_server.mobile_upload_observations(
                api_server.MobileObservationUploadRequest(
                    deviceCode="device-a",
                    leaseToken=first["leaseToken"],
                    observations=[
                        {
                            "observationId": "station:stale",
                            "stationId": "station-1",
                            "taskId": first["id"],
                            "payload": {"stationName": "旧设备结果"},
                        }
                    ],
                ),
                authorization=f"Bearer {first_token}",
            )
        self.assertEqual([], stale["acceptedIds"])
        self.assertEqual(["station:stale"], stale["failedIds"])
        with patch.object(api_server, "MOBILE_LEASE_SECONDS", 60):
            accepted = api_server.mobile_upload_observations(
                api_server.MobileObservationUploadRequest(
                    deviceCode="device-b",
                    leaseToken=second["leaseToken"],
                    observations=[
                        {
                            "observationId": "station:ok",
                            "stationId": "station-1",
                            "taskId": second["id"],
                            "payload": {"stationName": "新设备结果"},
                        }
                    ],
                ),
                authorization=f"Bearer {self.devices['device-b']}",
            )
        self.assertEqual(["station:ok"], accepted["acceptedIds"])

        with patch.object(api_server, "MOBILE_LEASE_SECONDS", 60):
            api_server.mobile_progress_task(
                second["id"],
                api_server.MobileTaskActionRequest(
                    deviceCode="device-b",
                    leaseToken=second["leaseToken"],
                    progress={"stage": "uploaded"},
                ),
                authorization=f"Bearer {self.devices['device-b']}",
            )
            self.complete("device-b", second)
        duplicate = api_server.mobile_upload_observations(
            api_server.MobileObservationUploadRequest(
                deviceCode="device-b",
                leaseToken=second["leaseToken"],
                observations=[
                    {
                        "observationId": "station:ok",
                        "stationId": "station-1",
                        "taskId": second["id"],
                        "payload": {"stationName": "新设备结果"},
                    }
                ],
            ),
            authorization=f"Bearer {self.devices['device-b']}",
        )
        self.assertEqual(["station:ok"], duplicate["duplicateIds"])


if __name__ == "__main__":
    unittest.main()
