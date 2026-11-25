"""Read-only collection metrics and the collector's structured DeepSeek event log."""
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
import time
import fleet_control
from typing import Literal, Optional

from pydantic import BaseModel, Field

from site_exploration_bridge import quote_identifier


SCOPES = {"BAIDU_MIN240_499": "BAIDU", "AMAP_MIN240_647": "AMAP"}
PLATFORMS = tuple(SCOPES.values())
SHANGHAI = timezone(timedelta(hours=8))
BIN_SECONDS = 300
TREND_BINS = 24
WINDOWS = (10, 30, 60)
UNCLASSIFIED = "UNCLASSIFIED"
DEVICE_PLATFORMS = (*PLATFORMS, UNCLASSIFIED)


def midnight(stamp):
    return int(datetime.fromtimestamp(stamp, SHANGHAI).replace(hour=0, minute=0, second=0, microsecond=0).timestamp())


def read_ingestion(connect, history_table):
    table = quote_identifier(history_table, "dynamic history table")
    conn = connect()
    try:
        with conn.cursor() as cursor:
            cursor.execute("START TRANSACTION READ ONLY")
            cursor.execute("SELECT UNIX_TIMESTAMP() AS cutoff")
            cutoff = int(cursor.fetchone()["cutoff"])
            # Count receipts from each current round's formal task IDs, including
            # receipts before midnight. Supplement IDs are in a separate table.
            cursor.execute(f"""
                SELECT b.id AS batch_id, b.scope_key, s.source_key, r.id AS round_id,
                       TIMESTAMPDIFF(SECOND, '1970-01-01 00:00:00', r.started_at) AS round_started_at,
                       COUNT(h.id) AS round_snapshots
                  FROM station_monitor_batch b
                  LEFT JOIN station_monitor_batch_station s ON s.batch_id = b.id
                  LEFT JOIN station_monitor_round r ON r.batch_id = b.id AND r.round_no = b.current_round
                  LEFT JOIN station_monitor_task t ON t.round_id = r.id AND t.source_key = s.source_key
                  LEFT JOIN {table} h ON h.task_id = t.local_task_id AND h.source_key = t.source_key
                                      AND h.received_at > 0 AND h.received_at < %s
                 WHERE b.status = 'ACTIVE' AND b.scope_key IN (%s, %s)
                 GROUP BY b.id, b.scope_key, s.source_key, r.id, r.started_at
            """, (cutoff, *SCOPES))
            membership = cursor.fetchall()
            cursor.execute("""
                SELECT 1 FROM information_schema.tables WHERE table_schema = DATABASE()
                   AND table_name = 'station_monitor_supplement_task'
            """)
            has_supplements = cursor.fetchone() is not None
            # Join on the task and source key. A supplement runs on its target platform.
            task_joins = """
                LEFT JOIN station_monitor_task t ON t.local_task_id = h.task_id AND t.source_key = h.source_key
                LEFT JOIN station_monitor_batch b ON b.id = t.batch_id
            """
            scope = "b.scope_key"
            supplement_batch = "NULL"
            if has_supplements:
                task_joins += """
                    LEFT JOIN station_monitor_supplement_task s ON s.local_task_id = h.task_id
                    LEFT JOIN station_monitor_task origin ON origin.local_task_id = s.origin_task_id
                        AND origin.batch_id = s.origin_batch_id AND origin.round_id = s.origin_round_id
                        AND origin.source_key = h.source_key
                    LEFT JOIN station_monitor_batch ob ON ob.id = origin.batch_id
                """
                scope = """CASE WHEN s.local_task_id IS NULL THEN b.scope_key
                           WHEN ob.scope_key IN (%s, %s) AND s.target_scope_key <> ob.scope_key
                           THEN s.target_scope_key END"""
                supplement_batch = "origin.batch_id"
            scope_args = tuple(SCOPES) if has_supplements else ()
            earliest = min(midnight(cutoff), cutoff - BIN_SECONDS * TREND_BINS)
            cursor.execute(f"""
                SELECT h.source_key, h.device_id, h.received_at, {scope} AS collector_scope,
                       {supplement_batch} AS supplement_batch
                  FROM {table} h {task_joins}
                 WHERE h.received_at >= %s AND h.received_at < %s
            """, (*scope_args, earliest, cutoff))
            receipts = cursor.fetchall()
            cursor.execute(f"""
                SELECT h.source_key, h.device_id, {scope} AS collector_scope, MAX(h.received_at) AS received_at
                  FROM {table} h {task_joins}
                 WHERE h.received_at > 0 AND h.received_at < %s
                 GROUP BY h.source_key, h.device_id, collector_scope
            """, (*scope_args, cutoff))
            latest = cursor.fetchall()
        return summarize_ingestion(cutoff, membership, receipts, latest, has_supplements)
    finally:
        try:
            conn.rollback()
        finally:
            conn.close()


def summarize_round_coverage(membership, confirmed, ready):
    platforms = dict.fromkeys(PLATFORMS)
    for row in membership:
        platform = SCOPES[row["scope_key"]]
        if not ready[platform] or row.get("round_id") is None:
            continue
        if platforms[platform] is None:
            scope_size = len(confirmed[platform])
            platforms[platform] = {"startedAt": row["round_started_at"], "stations": 0,
                                   "stationsInScope": scope_size, "outstanding": scope_size, "snapshots": 0}
        if row["source_key"] in confirmed[platform] and row["round_snapshots"] > 0:
            stats = platforms[platform]
            stats["stations"] += 1
            stats["outstanding"] -= 1
            stats["snapshots"] += row["round_snapshots"]
    total = None
    if all(stats is not None for stats in platforms.values()):
        total = {key: sum(stats[key] for stats in platforms.values())
                 for key in ("stations", "stationsInScope", "outstanding", "snapshots")}
    return {"platforms": platforms, "total": total}


def summarize_ingestion(cutoff, membership, receipts, latest, has_supplements):
    batches = {platform: set() for platform in PLATFORMS}
    members = {platform: set() for platform in PLATFORMS}
    for row in membership:
        platform = SCOPES[row["scope_key"]]
        batches[platform].add(row["batch_id"])
        if row["source_key"]:
            members[platform].add(row["source_key"])
    ready = {platform: len(batches[platform]) == 1 for platform in PLATFORMS}
    overlap = set.intersection(*members.values())
    confirmed = {platform: members[platform] - overlap if ready[platform] else set() for platform in PLATFORMS}
    ownership = {key: platform for platform, keys in confirmed.items() for key in keys}
    unassigned = set.union(*members.values()) - ownership.keys()
    today_start = midnight(cutoff)
    trend_start = cutoff - TREND_BINS * BIN_SECONDS
    trend = [{"endAt": trend_start + (i + 1) * BIN_SECONDS, "BAIDU": 0, "AMAP": 0} for i in range(TREND_BINS)]
    platform_rows = defaultdict(list)
    collector_rows = defaultdict(list)
    by_device = defaultdict(list)
    supplement_rows = defaultdict(list)
    for row in receipts:
        stamp = row["received_at"]
        if stamp < min(today_start, trend_start) or stamp >= cutoff:
            continue
        key = row["source_key"]
        owner = ownership.get(key, "UNASSIGNED" if key in unassigned else "OUTSIDE")
        platform_rows[owner].append(row)
        if owner not in PLATFORMS:
            continue
        collector = SCOPES.get(row["collector_scope"])
        collector_rows[collector].append(row)
        by_device[row["device_id"] or ""].append((collector, row))
        if collector and stamp >= trend_start:
            trend[(stamp - trend_start) // BIN_SECONDS][collector] += 1
        if collector and collector != owner and row["supplement_batch"] in batches[owner]:
            supplement_rows[owner].append(row)

    def count(rows):
        return {"snapshots": len(rows), "stations": len({r["source_key"] for r in rows if r["source_key"]})}

    def window_counts(rows):
        return {str(n): count([r for r in rows if r["received_at"] >= cutoff - 60 * n]) for n in WINDOWS}

    def today(rows):
        return count([r for r in rows if r["received_at"] >= today_start])

    def section(rows, stations):
        daily = today(rows)
        return {"windows": window_counts(rows), "today": {**daily, "stationsInScope": stations,
                "outstanding": stations - daily["stations"]}}

    latest_device = {}
    latest_collector = {}
    latest_by_device_platform = defaultdict(dict)
    for row in latest:
        if row["source_key"] in ownership:
            stamp = row["received_at"]
            device = row["device_id"] or ""
            latest_device[device] = max(stamp, latest_device.get(device, 0))
            collector = SCOPES.get(row["collector_scope"])
            platform = collector or UNCLASSIFIED
            device_latest = latest_by_device_platform[device]
            device_latest[platform] = max(stamp, device_latest.get(platform, 0))
            if collector:
                latest_collector[collector] = max(stamp, latest_collector.get(collector, 0))
    all_formal_rows = [r for platform in PLATFORMS for r in platform_rows[platform]]
    all_ready = all(ready.values())
    devices = {}
    for device in (by_device.keys() | latest_device.keys()):
        rows = by_device[device]
        devices[device] = {
            "windows": {p: window_counts([r for collector, r in rows if collector == p]) for p in PLATFORMS},
            "unclassified": window_counts([r for collector, r in rows if collector is None]),
            "total": window_counts([r for _, r in rows]),
            "latestAt": latest_device.get(device),
            "latestByPlatform": latest_by_device_platform.get(device, {}),
        }
    return {
        "cutoff": cutoff, "dayStart": today_start,
        "batches": {p: sorted(batches[p]) for p in PLATFORMS},
        "platforms": {p: section(platform_rows[p], len(confirmed[p])) if ready[p] else None for p in PLATFORMS},
        "total": section(all_formal_rows, len(ownership)) if all_ready else None,
        "roundCoverage": summarize_round_coverage(membership, confirmed, ready),
        "speed": {
            "platforms": {p: {"windows": window_counts(collector_rows[p]), "latestAt": latest_collector.get(p)}
                          for p in PLATFORMS},
            "total": {"windows": window_counts(collector_rows["BAIDU"] + collector_rows["AMAP"])},
            "unclassified": {"windows": window_counts(collector_rows[None]), "today": today(collector_rows[None])},
        } if all_ready else None,
        "trend": trend if all_ready else None,
        "devices": devices,
        "overlapStations": len(overlap), "unassignedStations": len(unassigned),
        "outside": {"windows": window_counts(platform_rows["OUTSIDE"]), "today": today(platform_rows["OUTSIDE"]) } if all_ready else None,
        "unassigned": {"windows": window_counts(platform_rows["UNASSIGNED"]), "today": today(platform_rows["UNASSIGNED"])},
        "supplement": {p: {"windows": window_counts(supplement_rows[p]), "today": today(supplement_rows[p])}
                       for p in PLATFORMS} if all_ready and has_supplements else None,
    }


class MonitorPhone(BaseModel):
    id: str = Field(min_length=1)  # ADB serial when available, otherwise a local inventory identifier.
    name: str = Field(min_length=1)
    clients: dict[Literal["BAIDU", "AMAP"], list[str]] = Field(default_factory=dict)


class MonitorDeviceRoster(BaseModel):
    phones: list[MonitorPhone] = Field(default_factory=list)
    excluded: list[str] = Field(default_factory=list)


def read_device_view(ingestion, operations, db_path):
    path = Path(db_path).with_name("collector-monitor-devices.json")
    try:
        roster = MonitorDeviceRoster.model_validate_json(path.read_bytes())
        if len({phone.id for phone in roster.phones}) != len(roster.phones):
            raise ValueError("Duplicate phone identifiers in the collector monitor roster")
    except FileNotFoundError:
        roster = MonitorDeviceRoster()
    except (OSError, ValueError):
        return {**build_device_view(ingestion, operations), "inventoryError": True}
    return build_device_view(ingestion, operations, roster)


def build_device_view(ingestion, operations, roster=None):
    """Resolve the physical phone separately from each app and its actual receipt platform."""
    roster = roster or MonitorDeviceRoster()
    registered = {row["id"]: row for row in (operations or {}).get("devices", [])}
    readings = (ingestion or {}).get("devices", {})
    can_measure = (ingestion or {}).get("total") is not None
    aliases = defaultdict(set)
    phones = {}

    def app(platform, expected=False):
        return {"platform": platform, "expected": expected, "clientIds": [], "registrationIds": [],
                "snapshots": {str(w): None for w in WINDOWS}, "latestAt": None}

    def phone(phone_id, name, expected):
        return {"id": phone_id, "name": name, "apps": {p: app(p, p in expected) for p in PLATFORMS}}

    def windows_for(reading, platform):
        if platform == UNCLASSIFIED:
            return reading.get("unclassified", {})
        return reading.get("windows", {}).get(platform, {})

    def add_client(lane, client_id, registration, reading):
        lane["clientIds"].append(client_id)
        if registration:
            lane["registrationIds"].append(client_id)
        if can_measure:
            windows = windows_for(reading, lane["platform"])
            for window in lane["snapshots"]:
                lane["snapshots"][window] = (lane["snapshots"][window] or 0) + windows.get(window, {}).get("snapshots", 0)
            last = reading.get("latestByPlatform", {}).get(lane["platform"])
            if last and last > (lane["latestAt"] or 0):
                lane["latestAt"] = last

    for entry in roster.phones:
        phones[entry.id] = phone(entry.id, entry.name, entry.clients)
        for platform, client_aliases in entry.clients.items():
            for client_alias in filter(None, client_aliases):
                aliases[client_alias].add((entry.id, platform))

    excluded = set(roster.excluded)
    longest_window = str(max(WINDOWS))
    unassigned = []
    for client_id in sorted(registered.keys() | readings.keys()):
        device = registered.get(client_id, {})
        reading = readings.get(client_id, {})
        code = device.get("deviceCode")
        diagnostic = client_id in excluded or (code is not None and code in excluded)
        if diagnostic:
            # Disregard diagnostic registration and heartbeat, but account for any actual receipts.
            device = {}
            code = None
        matches = aliases.get(client_id, set()) | aliases.get(code, set()) if not diagnostic else set()
        phone_ids = {identity for identity, _ in matches}
        serial = (device.get("adbSerial") or "").strip()
        if serial:
            phone_ids.add(serial)
        identity = next(iter(phone_ids)) if len(phone_ids) == 1 else None

        registered_platform = (device.get("platform") or UNCLASSIFIED) if device else None
        declared_platforms = {platform for _, platform in matches}
        if registered_platform is not None:
            declared_platforms.add(registered_platform)
        # Keep recent unmatched receipts visible, without treating old history-only IDs as new phones.
        show_client = bool(identity or device)
        for platform in DEVICE_PLATFORMS:
            recent = windows_for(reading, platform).get(longest_window, {}).get("snapshots", 0)
            has_history = platform in reading.get("latestByPlatform", {})
            if not (recent or show_client and (platform in declared_platforms or has_history)):
                continue
            if identity is not None:
                if identity not in phones:
                    phones[identity] = phone(identity, device.get("name") or "手机", ())
                apps = phones[identity]["apps"]
                if platform not in apps:
                    apps[platform] = app(platform)
                lane = apps[platform]
            else:
                lane = app(platform)
                unassigned.append(lane)
            # A heartbeat from one app cannot describe an upload on the other platform.
            add_client(lane, client_id, device if registered_platform == platform else None, reading)

    return {"phones": list(phones.values()), "unassigned": unassigned}


class RecoveryEvent(BaseModel):
    invocationId: str = Field(pattern=r"^[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}$", min_length=36, max_length=36)
    stage: Literal["OPEN_MAP", "SEARCH_INPUT", "SEARCH", "OPEN_DETAIL", "READ_DETAIL", "PRICE"]
    cause: Literal["POPUP", "UNEXPECTED_PAGE", "STAGE_TIMEOUT"]
    outcome: Literal["REQUESTED", "NO_ACTION", "REJECTED", "RESUMED", "NEEDS_REVIEW", "CANCELLED"]
    modelStatus: Literal["PENDING", "DECISION", "HTTP_ERROR", "NETWORK_ERROR", "INVALID_RESPONSE", "TIMEOUT", "CANCELLED"] = "PENDING"
    action: Optional[Literal["PAUSE_AND_BACK", "PAUSE_AND_CLOSE_OVERLAY", "CLICK_NODE", "NONE"]] = None
    modelMs: Optional[int] = Field(default=None, ge=0, le=120_000)
    httpStatus: Optional[int] = Field(default=None, ge=100, le=599)
    appliedActions: int = Field(default=0, ge=0, le=20)


def init_schema(conn):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS collector_recovery_event (
            device_id TEXT NOT NULL,
            invocation_id TEXT NOT NULL,
            received_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL,
            stage TEXT NOT NULL,
            cause TEXT NOT NULL,
            outcome TEXT NOT NULL,
            model_status TEXT NOT NULL,
            action TEXT,
            model_ms INTEGER,
            http_status INTEGER,
            applied_actions INTEGER NOT NULL,
            PRIMARY KEY (device_id, invocation_id)
        );
        CREATE INDEX IF NOT EXISTS idx_recovery_received ON collector_recovery_event(received_at);
    """)


def save_event(conn, device_id, event, now):
    with conn:
        conn.execute("""
            INSERT INTO collector_recovery_event VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(device_id, invocation_id) DO UPDATE SET
                updated_at=excluded.updated_at, outcome=excluded.outcome, model_status=excluded.model_status,
                action=excluded.action, model_ms=excluded.model_ms, http_status=excluded.http_status,
                applied_actions=excluded.applied_actions
            WHERE collector_recovery_event.outcome='REQUESTED' AND excluded.outcome<>'REQUESTED'
              AND collector_recovery_event.stage=excluded.stage AND collector_recovery_event.cause=excluded.cause
        """, (device_id, event.invocationId, now, now, event.stage, event.cause, event.outcome,
              event.modelStatus, event.action, event.modelMs, event.httpStatus, event.appliedActions))
        conn.execute("DELETE FROM collector_recovery_event WHERE received_at < ?", (now - 30 * 86400,))


def device_platform(device):
    try:
        package = json.loads(device["capabilities"]).get("targetPackage", "")
    except (AttributeError, ValueError, TypeError):
        package = ""
    return {"com.baidu.baidumap": "BAIDU", "com.autonavi.minimap": "AMAP"}.get(str(package).lower())


def heartbeat_recent(value, now):
    try:
        heartbeat = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if heartbeat.tzinfo is None:
            heartbeat = heartbeat.replace(tzinfo=timezone.utc)
        return 0 <= now - heartbeat.timestamp() <= 90
    except (AttributeError, ValueError):
        return False


def read_operations_db(db_path, **filters):
    uri = Path(db_path).resolve().as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=5)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("BEGIN")
        return read_operations(conn, **filters)
    finally:
        try:
            conn.rollback()
        finally:
            conn.close()


def read_operations(conn, now=None, outcome=None, device_id=None, before=None, log_days=1):
    """Read only metadata and bounded, structured recovery events from the mobile database."""
    if now is None:
        now = int(time.time())
    state = conn.execute("SELECT * FROM fleet_control WHERE id=1").fetchone()
    generation = state["generation"]
    devices = []
    for row in conn.execute("""
        SELECT d.*, COALESCE(c.allowed_generation, 0) AS allowed_gen,
               COALESCE(c.stopped_generation, 0) AS stopped_gen
          FROM collector_device d LEFT JOIN fleet_device_control c ON c.device_id = d.id
         ORDER BY d.name, d.id
    """):
        try:
            capabilities = json.loads(row["capabilities"]) or {}
        except (TypeError, ValueError):
            capabilities = {}
        if not isinstance(capabilities, dict):
            capabilities = {}
        control = fleet_control.snapshot(conn, row)
        devices.append({"id": row["id"], "name": row["name"], "platform": device_platform(row),
                        "deviceCode": row["device_code"] if "device_code" in row.keys() else None,
                        "adbSerial": row["adb_serial"] if "adb_serial" in row.keys() else None,
                        "appVersion": row["app_version"], "status": row["status"], "heartbeatAt": row["last_heartbeat_at"],
                        "online": heartbeat_recent(row["last_heartbeat_at"], now), "currentTaskId": row["current_task_id"],
                        "recoveryTelemetry": capabilities.get("deepSeekRecoveryTelemetry") is True,
                        "recoveryEnabled": capabilities.get("deepSeekRecoveryEnabled"),
                        "stopSupported": capabilities.get("fleetStopSupported") is True,
                        "remoteControlSupported": capabilities.get("remoteDeviceControl") == 1,
                        "collectionMode": capabilities.get("collectionMode"),
                        "runRequested": capabilities.get("collectionRunning"),
                        "accessibilityConnected": capabilities.get("accessibilityConnected"),
                        "lastError": row["last_error"] if "last_error" in row.keys() else "",
                        "command": control.get("deviceCommand"),
                        "stopRequired": control["stopRequired"],
                        "stopAcknowledged": (control["deviceCommand"]["status"] == "APPLIED" if control.get("deviceStopRequired")
                            else generation > 0 and row["stopped_gen"] >= generation)})
    pending = sum(d["stopRequired"] and not d["stopAcknowledged"] for d in devices)
    counts_today = Counter()
    attention = 0
    for row in conn.execute("""
        SELECT outcome, model_status, COUNT(*) AS total FROM collector_recovery_event
         WHERE received_at >= ? AND received_at <= ? GROUP BY outcome, model_status
    """, (midnight(now), now)):
        counts_today[row["outcome"]] += row["total"]
        if row["outcome"] in {"NEEDS_REVIEW", "REJECTED"} or (row["outcome"] == "NO_ACTION" and row["model_status"] in {
            "HTTP_ERROR", "NETWORK_ERROR", "INVALID_RESPONSE", "TIMEOUT"
        }):
            attention += row["total"]
    first = conn.execute("SELECT MIN(received_at) FROM collector_recovery_event").fetchone()[0]
    clauses = ["e.received_at >= ?", "e.received_at <= ?"]
    values = [midnight(now) - (log_days - 1) * 86400, now]
    if outcome:
        clauses.append("e.outcome = ?")
        values.append(outcome)
    if device_id:
        clauses.append("e.device_id = ?")
        values.append(device_id)
    if before is not None:
        clauses.append("e.rowid < ?")
        values.append(before)
    rows = conn.execute(f"""
        SELECT e.rowid AS event_id, e.*, d.name AS device_name FROM collector_recovery_event e
        LEFT JOIN collector_device d ON d.id = e.device_id
        WHERE {' AND '.join(clauses)} ORDER BY e.rowid DESC LIMIT 21
    """, values).fetchall()
    events = [dict(row) for row in rows[:20]]
    stop_result = conn.execute("SELECT response FROM fleet_stop_request WHERE request_id = ?", (state["request_id"],)).fetchone()
    released_tasks = (json.loads(stop_result["response"])["releasedTaskCount"] if stop_result and stop_result["response"]
                      else sum(json.loads(state["released_tasks"]).values()))
    return {"checkedAt": now, "devices": devices,
            "fleet": {"generation": generation, "requestedAt": state["requested_at"], "requestId": state["request_id"],
                      "allowedDevices": sum(not d["stopRequired"] for d in devices),
                      "releaseComplete": bool(state["release_complete"]), "pending": pending,
                      "acknowledged": sum(d["stopRequired"] and d["stopAcknowledged"] for d in devices),
                      "releasedTasks": released_tasks,
                      "retryRequestId": state["request_id"] if not state["release_complete"] else None},
            "recovery": {"model": "deepseek-v4-flash", "since": first, "todayCalls": sum(counts_today.values()),
                         "todayOutcomes": dict(counts_today), "todayAttention": attention, "events": events,
                         "nextBefore": events[-1]["event_id"] if len(rows) > 20 else None}}
