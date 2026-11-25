"""Offline checks for the dashboard, using the real HTTP routes and a temporary device DB."""
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import MagicMock, patch
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pymysql

import api_server
import collector_monitor as monitor
import fleet_control
import preview_collector_monitor


MONITOR = "/api/v1/collector-monitor"
VIEW_KEY = {"X-Collector-Monitor-Key": "isolated-test-key"}
BAIDU, AMAP = tuple(monitor.SCOPES)


def receipt(source, collector_scope, received_at, device_id="test-device", supplement_batch=None):
    return dict(source_key=source, collector_scope=collector_scope, received_at=received_at,
                device_id=device_id, supplement_batch=supplement_batch)


def membership():
    return [dict(batch_id=batch, scope_key=scope, source_key=source)
            for batch, scope, source in [(10, BAIDU, "B1"), (10, BAIDU, "B2"), (11, AMAP, "A1")]]


class CollectorIngestionTests(unittest.TestCase):
    def test_windows_and_coverage_use_distinct_scopes(self):
        cutoff = 1_800_000_000
        rows = [receipt("B1", BAIDU, cutoff - 1800), receipt("B1", BAIDU, cutoff - 1801),
                receipt("B1", AMAP, cutoff - 600, supplement_batch=10),
                receipt("A1", None, cutoff - 42), receipt("outside", AMAP, cutoff - 30),
                receipt("B2", BAIDU, cutoff)]
        stats = monitor.summarize_ingestion(cutoff, membership(), rows, rows[:-1], True)
        self.assertEqual({"snapshots": 2, "stations": 1}, stats["speed"]["total"]["windows"]["30"])
        self.assertEqual({"snapshots": 1, "stations": 1}, stats["speed"]["total"]["windows"]["10"])
        self.assertEqual(1, stats["speed"]["platforms"]["BAIDU"]["windows"]["30"]["snapshots"])
        self.assertEqual(1, stats["speed"]["platforms"]["AMAP"]["windows"]["30"]["snapshots"])
        self.assertEqual(1, stats["speed"]["unclassified"]["windows"]["30"]["snapshots"])
        self.assertEqual(1, stats["supplement"]["BAIDU"]["today"]["snapshots"])
        self.assertEqual(1, stats["outside"]["windows"]["30"]["snapshots"])
        self.assertEqual({"snapshots": 4, "stations": 2, "stationsInScope": 3, "outstanding": 1}, stats["total"]["today"])

    def test_round_coverage_keeps_prior_day_receipts_and_excludes_supplements(self):
        cutoff = monitor.midnight(1_800_000_000) + 3600
        started = cutoff - 7200
        members = [{**row, "round_id": row["batch_id"], "round_started_at": started,
                    "round_snapshots": 2 if row["source_key"] == "B1" else 0}
                   for row in membership()]
        today = [receipt("B2", AMAP, cutoff - 10, supplement_batch=10)]
        stats = monitor.summarize_ingestion(cutoff, members, today, today, True)
        baidu = stats["roundCoverage"]["platforms"]["BAIDU"]
        self.assertEqual({"startedAt": started, "snapshots": 2, "stations": 1,
                          "stationsInScope": 2, "outstanding": 1}, baidu)
        self.assertEqual(1, stats["total"]["today"]["snapshots"])
        self.assertEqual(1, stats["supplement"]["BAIDU"]["today"]["stations"])
        self.assertEqual(0, stats["roundCoverage"]["platforms"]["AMAP"]["stations"])

        for member in members:
            if member["scope_key"] == BAIDU:
                member.update(round_id=14, round_started_at=cutoff, round_snapshots=0)
        rolled = monitor.summarize_ingestion(cutoff, members, today, today, True)
        self.assertEqual({"snapshots": 0, "stations": 0, "stationsInScope": 3, "outstanding": 3},
                         rolled["roundCoverage"]["total"])
        self.assertEqual(1, rolled["total"]["today"]["stations"])

    def test_hourly_window_matches_recent_trend_bins_across_midnight(self):
        cutoff = monitor.midnight(1_800_000_000) + 60
        rows = [receipt("B1", BAIDU, cutoff - 3601), receipt("B1", BAIDU, cutoff - 3600),
                receipt("B1", AMAP, cutoff - 2700), receipt("A1", AMAP, cutoff - 1),
                receipt("A1", None, cutoff - 2), receipt("outside", BAIDU, cutoff - 3),
                receipt("B2", BAIDU, cutoff)]
        stats = monitor.summarize_ingestion(cutoff, membership(), rows, rows[:-1], True)
        self.assertEqual({"snapshots": 3, "stations": 2}, stats["speed"]["total"]["windows"]["60"])
        self.assertEqual(1, stats["speed"]["total"]["windows"]["30"]["snapshots"])
        bins = stats["trend"][-12:]
        self.assertEqual(cutoff, bins[-1]["endAt"])
        for platform in monitor.PLATFORMS:
            self.assertEqual(stats["speed"]["platforms"][platform]["windows"]["60"]["snapshots"],
                             sum(bin[platform] for bin in bins))

    def test_zero_receipts_and_unverified_membership(self):
        cutoff = 1_800_000_000
        historical = receipt("B2", BAIDU, cutoff - 900_000)
        zero = monitor.summarize_ingestion(cutoff, membership(), [], [historical], True)
        self.assertEqual(0, zero["speed"]["total"]["windows"]["30"]["snapshots"])
        self.assertEqual(3, zero["total"]["today"]["outstanding"])
        self.assertEqual(historical["received_at"], zero["speed"]["platforms"]["BAIDU"]["latestAt"])
        self.assertIsNone(zero["speed"]["platforms"]["AMAP"]["latestAt"])
        conflicting = [*membership(), dict(batch_id=12, scope_key=BAIDU, source_key="A1")]
        ambiguous = monitor.summarize_ingestion(cutoff, conflicting, [], [], False)
        self.assertIsNone(ambiguous["total"])
        self.assertIsNone(ambiguous["speed"])
        self.assertEqual(1, ambiguous["overlapStations"])
        self.assertEqual(3, ambiguous["unassignedStations"])

    def test_ingestion_cleanup_on_query_failure(self):
        connection = MagicMock()
        cursor = connection.cursor.return_value.__enter__.return_value
        cursor.execute.side_effect = [None, pymysql.OperationalError(2013, "simulated disconnect")]
        with self.assertRaises(pymysql.OperationalError):
            monitor.read_ingestion(lambda: connection, "site_exploration_charging_station_dynamic_history")
        self.assertEqual("START TRANSACTION READ ONLY", cursor.execute.call_args_list[0].args[0])
        connection.rollback.assert_called_once()
        connection.close.assert_called_once()


class CollectorDeviceViewTests(unittest.TestCase):
    def test_physical_phones_keep_app_metrics_and_registration_independent(self):
        cutoff = 1_800_000_000
        receipts = [receipt("B1", BAIDU, cutoff - 90, "baidu-1"),
                    receipt("B2", BAIDU, cutoff - 900, "baidu-1"),
                    receipt("B1", AMAP, cutoff - 80, "amap-1", supplement_batch=10),
                    receipt("A1", AMAP, cutoff - 70, "baidu-1"),
                    receipt("A1", BAIDU, cutoff - 2700, "unmapped", supplement_batch=11),
                    receipt("A1", AMAP, cutoff - 4000, "amap-2")]
        ingestion = monitor.summarize_ingestion(cutoff, membership(), receipts, receipts, True)
        operations = {"devices": [
            {"id": "baidu-1", "platform": "BAIDU", "name": "K60", "adbSerial": "phone-1"},
            {"id": "amap-1", "platform": "AMAP", "name": "K60", "deviceCode": "AMAP-01"},
            {"id": "amap-2", "platform": "AMAP", "name": "MI 8 Lite", "deviceCode": "AMAP-02"},
            {"id": "amap-3", "platform": "AMAP", "name": "MI 8 Lite", "adbSerial": "phone-3"},
            {"id": "diag", "platform": "AMAP", "deviceCode": "EV-DIAG"},
        ]}
        roster = monitor.MonitorDeviceRoster(phones=[
            monitor.MonitorPhone(id="phone-1", name="K60", clients={"BAIDU": ["baidu-1"], "AMAP": ["AMAP-01"]}),
            monitor.MonitorPhone(id="phone-2", name="MI 8 Lite", clients={"AMAP": ["AMAP-02"]}),
            monitor.MonitorPhone(id="phone-3", name="MI 8 Lite"),
        ], excluded=["EV-DIAG"])
        view = monitor.build_device_view(ingestion, operations, roster)
        self.assertEqual(["phone-1", "phone-2", "phone-3"], [p["id"] for p in view["phones"]])
        primary, older, zero = [p["apps"] for p in view["phones"]]
        self.assertEqual({"10": 1, "30": 2, "60": 2}, primary["BAIDU"]["snapshots"])
        self.assertEqual({"10": 2, "30": 2, "60": 2}, primary["AMAP"]["snapshots"])
        self.assertEqual(["amap-1"], primary["AMAP"]["registrationIds"])
        self.assertEqual(cutoff - 70, primary["AMAP"]["latestAt"])
        self.assertEqual(cutoff - 4000, older["AMAP"]["latestAt"])
        self.assertIsNone(older["BAIDU"]["snapshots"]["30"])
        self.assertEqual({"10": 0, "30": 0, "60": 0}, zero["AMAP"]["snapshots"])
        self.assertIsNone(zero["AMAP"]["latestAt"])
        self.assertEqual([["unmapped"]], [lane["clientIds"] for lane in view["unassigned"]])
        for platform in monitor.PLATFORMS:
            for window in map(str, monitor.WINDOWS):
                assigned = [phone["apps"][platform] for phone in view["phones"]]
                pending = [lane for lane in view["unassigned"] if lane["platform"] == platform]
                self.assertEqual(ingestion["speed"]["platforms"][platform]["windows"][window]["snapshots"],
                                 sum(lane["snapshots"][window] or 0 for lane in assigned + pending))

    def test_conflicting_identity_and_unavailable_ingestion_are_not_guessed(self):
        roster = monitor.MonitorDeviceRoster(phones=[
            monitor.MonitorPhone(id="phone-1", name="K60", clients={"BAIDU": ["baidu-1"]}),
        ])
        operations = {"devices": [{"id": "baidu-1", "platform": "BAIDU", "adbSerial": "phone-2"}]}
        view = monitor.build_device_view(None, operations, roster)
        self.assertEqual(["phone-1"], [p["id"] for p in view["phones"]])
        self.assertEqual([], view["phones"][0]["apps"]["BAIDU"]["registrationIds"])
        self.assertEqual(["baidu-1"], view["unassigned"][0]["registrationIds"])
        self.assertEqual({"10": None, "30": None, "60": None}, view["unassigned"][0]["snapshots"])

        cutoff = 1_800_000_000
        received = [receipt("B1", BAIDU, cutoff - 90, "baidu-1")]
        ingestion = monitor.summarize_ingestion(cutoff, membership(), received, received, True)
        with tempfile.TemporaryDirectory() as path:
            db = Path(path) / "mobile.db"
            roster_path = db.with_name("collector-monitor-devices.json")
            roster_path.write_text(roster.model_dump_json(), encoding="utf-8")
            without_ops = monitor.read_device_view(ingestion, None, db)["phones"][0]["apps"]["BAIDU"]
            self.assertEqual([], without_ops["registrationIds"])
            self.assertEqual(1, without_ops["snapshots"]["30"])
            roster_path.write_text("{", encoding="utf-8")
            invalid = monitor.read_device_view(ingestion, None, db)
            self.assertTrue(invalid["inventoryError"])
            self.assertEqual(1, invalid["unassigned"][0]["snapshots"]["30"])


class CollectorMonitorHttpTests(unittest.TestCase):
    def setUp(self):
        temp = self.enterContext(tempfile.TemporaryDirectory())
        self.enterContext(patch.object(api_server, "MOBILE_DB_PATH", str(Path(temp) / "mobile.db")))
        self.enterContext(patch.object(api_server, "MOBILE_ACTIVATION_CODE", "isolated-test-device"))
        self.enterContext(patch.dict(os.environ, {"COLLECTOR_MONITOR_KEY": VIEW_KEY["X-Collector-Monitor-Key"]}))
        self.enterContext(patch.object(api_server, "_mysql_configured", return_value=False))
        self.enterContext(patch.object(api_server, "_monitor_ingestion", return_value={"cutoff": 1_800_000_000}))
        api_server.init_mobile_db()
        # Reuse the exact application routes without the production startup hook.
        app = FastAPI()
        app.router.routes.extend(route for route in api_server.app.routes
                                 if getattr(route, "path", "").startswith(("/station-dashboard", "/collector-monitor", MONITOR))
                                 or getattr(route, "path", "") == "/api/v1/devices/me/stopped")
        self.client = self.enterContext(TestClient(app))

    def register(self, platform):
        code = "BD-test" if platform == "BAIDU" else "AM-test"
        package = "com.baidu.BaiduMap" if platform == "BAIDU" else "com.autonavi.minimap"
        return api_server.mobile_register(api_server.MobileRegisterRequest(
            activationCode="isolated-test-device", deviceCode=code, name=code, appVersion="test",
            capabilities={"targetPackage": package, "fleetStopSupported": True,
                          "deepSeekRecoveryTelemetry": platform == "BAIDU"},
        ))

    def operations(self, **params):
        return self.client.get(MONITOR, headers=VIEW_KEY, params=params).json()["operations"]

    def test_old_and_new_entry_and_operator_auth(self):
        old, alias = (self.client.get(path) for path in ("/station-dashboard", "/collector-monitor"))
        self.assertEqual(200, old.status_code)
        self.assertEqual(old.text, alias.text)
        self.assertIn("/station-dashboard/monitor.js", old.text)
        self.assertIn("frame-ancestors 'none'", old.headers["Content-Security-Policy"])
        for asset in ("monitor.js", "monitor.css", "public-sans.woff2"):
            self.assertEqual(200, self.client.get(f"/station-dashboard/{asset}").status_code)
        self.assertEqual(404, self.client.get("/station-dashboard/api_server.py").status_code)
        self.assertEqual(401, self.client.get(MONITOR).status_code)
        self.assertEqual(401, self.client.post(MONITOR + "/stop-all", json={"requestId": "denied"}).status_code)
        with patch.dict(os.environ, {"COLLECTOR_MONITOR_KEY": ""}):
            self.assertEqual(503, self.client.get(MONITOR, headers=VIEW_KEY).status_code)
        response = self.client.get(MONITOR, headers=VIEW_KEY)
        self.assertEqual("no-store", response.headers["Cache-Control"])
        self.assertEqual(0, response.json()["operations"]["fleet"]["generation"])
        self.assertEqual({"phones": [], "unassigned": []}, response.json()["deviceView"])

    def test_preview_switch_keeps_demo_and_never_registers_stop(self):
        with TestClient(preview_collector_monitor.app, base_url="http://127.0.0.1") as client:
            home = client.get("/?demo=1", follow_redirects=False)
            self.assertEqual("/station-dashboard?preview=1&demo=1", home.headers["Location"])
            self.assertEqual(200, client.get(home.headers["Location"]).status_code)
            self.assertEqual(404, client.post(MONITOR + "/stop-all", json={"requestId": "never"}).status_code)

    def test_recovery_requires_device_token_deduplicates_and_paginates(self):
        auth = {"Authorization": "Bearer " + self.register("BAIDU")["deviceToken"]}
        amap = {"Authorization": "Bearer " + self.register("AMAP")["deviceToken"]}
        endpoint = MONITOR + "/recovery-events"
        event = {"invocationId": str(uuid4()), "stage": "SEARCH_INPUT", "cause": "STAGE_TIMEOUT", "outcome": "REQUESTED"}
        self.assertEqual(401, self.client.post(endpoint, json=event, headers=VIEW_KEY).status_code)
        self.assertEqual(403, self.client.post(endpoint, json=event, headers=amap).status_code)
        self.assertEqual(422, self.client.post(endpoint, json={**event, "stage": "unstructured"}, headers=auth).status_code)
        finish = {**event, "outcome": "RESUMED", "modelStatus": "DECISION", "action": "PAUSE_AND_BACK", "appliedActions": 1}
        for payload in (event, finish, event, finish):
            self.assertEqual(204, self.client.post(endpoint, json=payload, headers=auth).status_code)
        conn = api_server.mobile_conn()
        try:
            now = int(time.time())
            for _ in range(20):
                monitor.save_event(conn, self.operations()["devices"][1]["id"],
                                   monitor.RecoveryEvent(**{**event, "invocationId": str(uuid4())}), now)
        finally:
            conn.close()
        log = self.operations()["recovery"]
        self.assertEqual(21, log["todayCalls"])
        self.assertEqual(1, log["todayOutcomes"]["RESUMED"])
        self.assertEqual(20, len(log["events"]))
        older = self.operations(before=log["nextBefore"])["recovery"]
        self.assertEqual([event["invocationId"]], [e["invocation_id"] for e in older["events"]])
        device_id = older["events"][0]["device_id"]
        filtered = self.operations(outcome="RESUMED", device_id=device_id)["recovery"]
        self.assertEqual(21, filtered["todayCalls"])
        self.assertEqual(["DECISION"], [e["model_status"] for e in filtered["events"]])

    def test_partial_stop_retries_same_generation_and_reports_ack(self):
        baidu = self.register("BAIDU")
        self.register("AMAP")
        stop = MONITOR + "/stop-all"
        command = {"requestId": "monitor-stop-test"}
        mysql = MagicMock()
        conn = api_server.mobile_conn()
        try:
            with conn:
                now = api_server._mobile_utc()
                conn.execute("INSERT INTO scan_task(id,type,status,attempt,created_at,available_at,lease_token) "
                             "VALUES(?, 'SITE_STATION_DETAIL','RUNNING',1,?,?, 'old-lease')", ("active", now, now))
        finally:
            conn.close()
        with patch.object(api_server, "_mysql_configured", return_value=True), \
             patch.object(api_server, "_henan_mysql_conn", return_value=mysql), \
             patch.object(fleet_control, "release_mysql_leases", side_effect=[
                 pymysql.OperationalError(2013, "simulated disconnect"), {"station_monitor_task": 2},
             ]) as release:
            self.assertEqual(503, self.client.post(stop, headers=VIEW_KEY, json=command).status_code)
            partial = self.operations()["fleet"]
            self.assertFalse(partial["releaseComplete"])
            self.assertEqual(command["requestId"], partial["retryRequestId"])
            self.assertEqual(409, self.client.post(stop, headers=VIEW_KEY, json={"requestId": "other"}).status_code)
            accepted = self.client.post(stop, headers=VIEW_KEY, json=command).json()
            self.assertEqual(accepted, self.client.post(stop, headers=VIEW_KEY, json=command).json())
            self.assertEqual(2, release.call_count)
        self.assertEqual(partial["generation"], accepted["stopGeneration"])
        self.assertEqual(3, accepted["releasedTaskCount"])
        self.assertEqual(2, self.operations()["fleet"]["pending"])
        ack = self.client.post("/api/v1/devices/me/stopped", json={"stopGeneration": accepted["stopGeneration"]},
                               headers={"Authorization": "Bearer " + baidu["deviceToken"]})
        self.assertEqual(200, ack.status_code)
        fleet = self.operations()["fleet"]
        self.assertEqual((1, 1), (fleet["acknowledged"], fleet["pending"]))


if __name__ == "__main__":
    unittest.main()
