"""Loopback-only monitor preview with live reads and a separate sample view."""
import random
import sqlite3
import time
from datetime import datetime, timezone
from typing import Literal, Optional

import pymysql
import uvicorn
from fastapi import FastAPI, Query, Response
from fastapi.responses import RedirectResponse
from starlette.middleware.trustedhost import TrustedHostMiddleware

import api_server
import collector_monitor as monitor
import fleet_control


app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost"])
@app.get("/")
@app.get("/collector-monitor")
@app.get("/station-dashboard")
def preview_page(preview: bool = False, demo: bool = False):
    if not preview:
        return RedirectResponse("/station-dashboard?preview=1" + ("&demo=1" if demo else ""))
    return api_server.collector_monitor_page()


for path in ("/station-dashboard/{asset}", "/collector-monitor/{asset}"):
    app.add_api_route(path, api_server.collector_monitor_asset, methods=["GET"])


def demo_receipts(now):
    rng = random.Random(42)
    devices = [("demo-b1", "百度设备 01", "com.baidu.BaiduMap"),
               ("demo-b2", "百度设备 02", "com.baidu.BaiduMap"),
               ("demo-a1", "高德设备 01", "com.autonavi.minimap"),
               ("demo-a2", "高德设备 02", "com.autonavi.minimap")]
    scopes = list(monitor.SCOPES)
    membership = [{"batch_id": batch, "scope_key": scope, "source_key": f"{batch}-{number}",
                   "round_id": batch, "round_started_at": monitor.midnight(now) - (batch - 9) * 3600,
                   "round_snapshots": 0}
                  for batch, scope, count in zip((10, 11), scopes, (1080, 138)) for number in range(count)]
    receipts = []
    for index, member in enumerate(membership):
        scope_index = scopes.index(member["scope_key"])
        stamp = monitor.midnight(now) + rng.randrange(max(1, now - monitor.midnight(now)))
        station_index = int(member["source_key"].split("-")[1])
        # Two active series, with a small number of repeats and cross-platform receipts.
        if station_index >= (265, 98)[scope_index]:
            continue
        device_index = rng.randrange(2) if scope_index == 0 else 2
        cross = (scope_index == 0 and index % 61 == 0) or (scope_index == 1 and station_index == 0)
        if cross:
            device_index = 2 if scope_index == 0 else 0
        row = {"source_key": member["source_key"], "device_id": devices[device_index][0], "received_at": stamp,
               "collector_scope": scopes[1 - scope_index if cross else scope_index],
               "supplement_batch": member["batch_id"] if cross else None}
        receipts.append(row)
        if not cross:
            member["round_snapshots"] += 1
        if station_index % 23 == 0:
            receipts.append({**row, "received_at": now - rng.randint(25, 3800)})
            if not cross:
                member["round_snapshots"] += 1
    return monitor.summarize_ingestion(now, membership, receipts, receipts, True), devices


def demo_operations(devices, now, **filters):
    import json

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("""CREATE TABLE collector_device(id TEXT PRIMARY KEY, name TEXT,
            capabilities TEXT, app_version TEXT, status TEXT, last_heartbeat_at TEXT, current_task_id TEXT,
            device_code TEXT, adb_serial TEXT, last_error TEXT, updated_at TEXT)""")
        fleet_control.init_schema(conn)
        monitor.init_schema(conn)
        for index, (device_id, name, package) in enumerate(devices):
            capabilities = {"targetPackage": package, "fleetStopSupported": True, "remoteDeviceControl": 1,
                            "collectionRunning": index < 3, "accessibilityConnected": True,
                            "collectionMode": "DEVICE_DISPATCH" if index < 2 else "PRIORITY_DETAIL"}
            if index < 2:
                capabilities.update(deepSeekRecoveryTelemetry=True, deepSeekRecoveryEnabled=True)
            heartbeat = datetime.fromtimestamp(now - (900 if index == 3 else 15), timezone.utc).isoformat()
            conn.execute("INSERT INTO collector_device VALUES(?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?)",
                         (device_id, name, json.dumps(capabilities), "0.1.75" if index < 2 else "0.1.42.28",
                          "RUNNING" if index < 3 else "IDLE", heartbeat,
                          ("BD-" if index < 2 else "AM-") + device_id, "", "", heartbeat))
        pattern = [("SEARCH_INPUT", "STAGE_TIMEOUT", "RESUMED", "DECISION", "PAUSE_AND_BACK"),
                   ("OPEN_DETAIL", "POPUP", "RESUMED", "DECISION", "CLICK_NODE"),
                   ("SEARCH", "UNEXPECTED_PAGE", "REJECTED", "DECISION", "PAUSE_AND_BACK"),
                   ("SEARCH_INPUT", "STAGE_TIMEOUT", "NO_ACTION", "TIMEOUT", None)]
        for index in reversed(range(24)):
            stage, cause, outcome, model, action = pattern[index % len(pattern)]
            event = monitor.RecoveryEvent(invocationId=f"00000000-0000-4000-8000-{index:012x}",
                stage=stage, cause=cause, outcome=outcome, modelStatus=model, action=action,
                modelMs=10_000 if model == "TIMEOUT" else 810 + index * 37,
                appliedActions=int(outcome == "RESUMED"))
            monitor.save_event(conn, devices[index % 2][0], event, now - 45 - index * 95)
        return monitor.read_operations(conn, now=now, **filters)
    finally:
        conn.close()


def live_ingestion():
    try:
        return api_server._monitor_ingestion()
    except (pymysql.MySQLError, RuntimeError):
        return None


def live_operations(**filters):
    try:
        return monitor.read_operations_db(api_server.MOBILE_DB_PATH, **filters)
    except sqlite3.Error:
        return None


@app.get("/api/v1/collector-monitor")
def snapshot(response: Response, demo: bool = False, outcome: Optional[str] = Query(None, max_length=32),
             device_id: Optional[str] = Query(None, max_length=64), before: Optional[int] = Query(None, gt=0),
             log_days: Literal["1", "7"] = Query("1")):
    response.headers["Cache-Control"] = "no-store"
    filters = dict(outcome=outcome, device_id=device_id, before=before, log_days=int(log_days))
    if demo:
        now = int(time.time())
        ingestion, devices = demo_receipts(now)
        operations = demo_operations(devices, now, **filters)
        roster = monitor.MonitorDeviceRoster(phones=[
            monitor.MonitorPhone(id="sample-01", name="K60", clients={"BAIDU": ["demo-b1"], "AMAP": ["demo-a2"]}),
            monitor.MonitorPhone(id="sample-02", name="小米 8 青春版", clients={"BAIDU": ["demo-b2"]}),
            monitor.MonitorPhone(id="sample-03", name="小米 8 青春版", clients={"AMAP": ["demo-a1"]}),
        ])
        device_view = monitor.build_device_view(ingestion, operations, roster)
        description = "演示数据 · 入库、设备和恢复日志均为样例"
    else:
        ingestion = live_ingestion()
        operations = live_operations(**filters)
        device_view = monitor.read_device_view(ingestion, operations, api_server.MOBILE_DB_PATH)
        description = "本机只读预览 · 入库来自数据库，心跳来自本机登记"
    return {"ingestion": ingestion, "operations": operations, "deviceView": device_view,
            "preview": description, "stopDisabled": True}


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8811, access_log=False)
