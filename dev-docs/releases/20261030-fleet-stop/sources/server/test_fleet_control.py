"""Offline tests: stop/release barrier, stale leases, and manual device resume."""
import os
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from fastapi import HTTPException
import api_server
import fleet_control
from test_mobile_dispatch import MobileDispatchTests


class FleetStopTests(unittest.TestCase):
    register = MobileDispatchTests.register
    add_tasks = MobileDispatchTests.add_tasks
    claim = MobileDispatchTests.claim
    complete = MobileDispatchTests.complete

    def setUp(self):
        MobileDispatchTests.setUp(self)
        self.controller = patch.dict(os.environ, {"MOBILE_FLEET_CONTROLLER_DEVICE_CODES": "BD-controller"})
        self.controller.start()
        self.mysql = patch.object(api_server, "_mysql_configured", return_value=False)
        self.mysql.start()
        self.register("BD-controller")
        self.register("AM-device")

    def tearDown(self):
        self.mysql.stop()
        self.controller.stop()
        MobileDispatchTests.tearDown(self)

    def auth(self, code):
        return "Bearer " + self.devices[code]

    def stop(self, request_id="stop-one"):
        return api_server.mobile_stop_all(api_server.FleetStopRequest(requestId=request_id), self.auth("BD-controller"))

    def control(self, code):
        return api_server.mobile_config(self.auth(code))["fleetControl"]

    def resume(self, code, generation):
        return api_server.mobile_resume_device(api_server.FleetGenerationRequest(stopGeneration=generation), self.auth(code))

    def test_stop_releases_active_tasks_and_blocks_both_devices(self):
        self.add_tasks(3)
        old = self.claim("AM-device")
        self.claim("BD-controller")
        response = self.stop()
        self.assertEqual(2, response["releasedTaskCount"])
        self.assertIsNone(self.claim("AM-device"))
        self.assertIsNone(self.claim("BD-controller"))
        with self.assertRaises(HTTPException) as failure:
            self.complete("AM-device", old)
        self.assertEqual(409, failure.exception.status_code)
        conn = api_server.mobile_conn()
        rows = conn.execute("SELECT status, attempt, lease_token FROM scan_task").fetchall()
        conn.close()
        self.assertTrue(all(row["status"] == "PENDING" and row["attempt"] == 0 and not row["lease_token"] for row in rows))

    def test_manual_resume_applies_only_to_one_device_and_survives_restart(self):
        self.add_tasks(2)
        generation = self.stop()["stopGeneration"]
        api_server.init_mobile_db()
        self.register("AM-device")
        self.assertTrue(self.control("AM-device")["stopRequired"])
        self.resume("AM-device", generation)
        self.assertIsNotNone(self.claim("AM-device"))
        self.assertIsNone(self.claim("BD-controller"))

    def test_retry_same_request_is_idempotent_even_after_resume(self):
        response = self.stop()
        self.resume("AM-device", response["stopGeneration"])
        self.assertEqual(response, self.stop())
        self.assertFalse(self.control("AM-device")["stopRequired"])

    def test_old_resume_and_ack_cannot_override_a_new_stop(self):
        generation = self.stop()["stopGeneration"]
        current = self.stop("stop-two")["stopGeneration"]
        self.assertGreater(current, generation)
        with self.assertRaises(HTTPException):
            self.resume("AM-device", generation)
        with self.assertRaises(HTTPException):
            api_server.mobile_acknowledge_stop(api_server.FleetGenerationRequest(stopGeneration=generation), self.auth("AM-device"))
        self.assertTrue(self.control("AM-device")["stopRequired"])
        self.resume("AM-device", current)
        api_server.mobile_acknowledge_stop(api_server.FleetGenerationRequest(stopGeneration=current), self.auth("AM-device"))
        self.assertFalse(self.control("AM-device")["stopRequired"])

    def test_release_failure_keeps_claims_blocked_until_same_request_retries(self):
        self.add_tasks(1)
        self.claim("AM-device")
        conn = api_server.mobile_conn()
        fleet_control.begin_stop(conn, "stop-one", "test", api_server._mobile_utc())
        conn.close()
        self.assertFalse(self.control("AM-device")["releaseComplete"])
        self.assertIsNone(self.claim("AM-device"))
        with self.assertRaises(HTTPException):
            self.resume("AM-device", self.control("AM-device")["stopGeneration"])
        self.assertEqual(1, self.stop()["releasedTaskCount"])
        self.assertTrue(self.control("AM-device")["releaseComplete"])

    def test_refunds_recovery_attempt_and_preserves_completed_results(self):
        self.add_tasks(3)
        conn = api_server.mobile_conn()
        with conn:
            conn.execute("UPDATE scan_task SET status='RUNNING',attempt=3,recovery_attempt=2,lease_token='old' WHERE id='task-000'")
            conn.execute("UPDATE scan_task SET status='COMPLETED',attempt=1,result_summary='saved' WHERE id='task-001'")
            conn.execute("UPDATE scan_task SET status='LEASED',attempt=1 WHERE id='task-002'")
        conn.close()
        self.stop()
        conn = api_server.mobile_conn()
        rows = {row['id']:dict(row) for row in conn.execute("SELECT * FROM scan_task")}
        conn.close()
        self.assertEqual(('FAILED',3,1), tuple(rows['task-000'][key] for key in ('status','attempt','recovery_attempt')))
        self.assertEqual('saved', rows['task-001']['result_summary'])
        self.assertEqual('COMPLETED', rows['task-001']['status'])
        self.assertEqual(('PENDING',0), tuple(rows['task-002'][key] for key in ('status','attempt')))

    def test_only_controller_can_stop_all(self):
        with self.assertRaises(HTTPException) as failure:
            api_server.mobile_stop_all(api_server.FleetStopRequest(requestId="denied"), self.auth("AM-device"))
        self.assertEqual(403, failure.exception.status_code)
        self.assertEqual(0, self.control("AM-device")["stopGeneration"])

    def test_dispatch_guard_serializes_independent_connections(self):
        entered = threading.Event()
        def wait_for_guard():
            with fleet_control.dispatch_guard(api_server.MOBILE_DB_PATH):
                entered.set()
        with ThreadPoolExecutor(max_workers=1) as executor:
            with fleet_control.dispatch_guard(api_server.MOBILE_DB_PATH):
                pending = executor.submit(wait_for_guard)
                self.assertFalse(entered.wait(0.15))
            pending.result(timeout=3)
        self.assertTrue(entered.is_set())


if __name__ == '__main__':
    unittest.main()
