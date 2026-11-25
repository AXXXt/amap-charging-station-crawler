"""Durable, per-client start/stop commands; all callers serialize with dispatch_guard."""
import json


class DeviceControlError(Exception):
    pass


MODES = {"BAIDU": "DEVICE_DISPATCH", "AMAP": "PRIORITY_DETAIL"}


def init_schema(conn):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS collector_device_command (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            device_id TEXT NOT NULL,
            request_id TEXT NOT NULL UNIQUE,
            action TEXT NOT NULL,
            mode TEXT NOT NULL,
            fleet_generation INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'QUEUED',
            release_complete INTEGER NOT NULL DEFAULT 1,
            released_tasks TEXT NOT NULL DEFAULT '{}',
            requested_at TEXT NOT NULL,
            acknowledged_at TEXT NOT NULL DEFAULT '',
            error TEXT NOT NULL DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS idx_device_command_latest ON collector_device_command(device_id, id DESC);
    """)


def latest(conn, device_id):
    return conn.execute('SELECT * FROM collector_device_command WHERE device_id=? ORDER BY id DESC LIMIT 1',
                        (device_id,)).fetchone()


def payload(row):
    if row is None:
        return None
    counts = json.loads(row['released_tasks'])
    return {'commandId': row['id'], 'requestId': row['request_id'], 'deviceId': row['device_id'],
            'action': row['action'], 'mode': row['mode'], 'stopGeneration': row['fleet_generation'],
            'status': row['status'], 'releaseComplete': bool(row['release_complete']),
            'releasedTasks': counts, 'releasedTaskCount': sum(counts.values()),
            'requestedAt': row['requested_at'], 'acknowledgedAt': row['acknowledged_at'], 'error': row['error']}


def active(conn, device_id, generation):
    row = latest(conn, device_id)
    if row is None or row['status'] == 'CANCELLED' or row['fleet_generation'] != generation:
        return None
    return row


def begin(conn, device, platform, action, request_id, generation, now):
    existing = conn.execute('SELECT * FROM collector_device_command WHERE request_id=?', (request_id,)).fetchone()
    if existing:
        if existing['device_id'] != device['id'] or existing['action'] != action:
            raise DeviceControlError('REQUEST_ID_CONFLICT')
        return existing
    previous = latest(conn, device['id'])
    if previous and not previous['release_complete']:
        raise DeviceControlError('DEVICE_RELEASE_IN_PROGRESS_RETRY_PREVIOUS_REQUEST')
    if platform not in MODES:
        raise DeviceControlError('UNKNOWN_DEVICE_PLATFORM')
    with conn:
        conn.execute("UPDATE collector_device_command SET status='CANCELLED' WHERE device_id=? AND status='QUEUED'",
                     (device['id'],))
        cursor = conn.execute("""INSERT INTO collector_device_command
            (device_id, request_id, action, mode, fleet_generation, release_complete, requested_at)
            VALUES(?,?,?,?,?,?,?)""", (device['id'], request_id, action, MODES[platform], generation,
                                      int(action != 'STOP'), now))
    return conn.execute('SELECT * FROM collector_device_command WHERE id=?', (cursor.lastrowid,)).fetchone()


def cancel_for_manual_resume(conn, device_id):
    conn.execute("UPDATE collector_device_command SET status='CANCELLED' WHERE device_id=? AND status <> 'CANCELLED'",
                 (device_id,))


def cancel_for_fleet_stop(conn):
    with conn:
        conn.execute("UPDATE collector_device_command SET status='CANCELLED', release_complete=1 WHERE status <> 'CANCELLED'")


def release_mysql_leases(conn, device_id, now):
    # Reuse the established release semantics, with an explicit device filter on every update.
    import fleet_control
    return fleet_control.release_mysql_leases(conn, now, device_id=device_id)


def record_mysql_release(conn, command_id, counts):
    row = conn.execute('SELECT * FROM collector_device_command WHERE id=?', (command_id,)).fetchone()
    previous = json.loads(row['released_tasks'])
    for key, value in counts.items():
        previous[key] = previous.get(key, 0) + value
    with conn:
        conn.execute('UPDATE collector_device_command SET released_tasks=? WHERE id=?', (json.dumps(previous), command_id))


def finish_stop(conn, command_id, now):
    row = conn.execute('SELECT * FROM collector_device_command WHERE id=?', (command_id,)).fetchone()
    if row['status'] != 'QUEUED' or row['release_complete']:
        return payload(row)
    with conn:
        result = conn.execute("""UPDATE scan_task SET
            status=CASE WHEN recovery_attempt>0 THEN 'FAILED' ELSE 'PENDING' END,
            attempt=CASE WHEN recovery_attempt=0 THEN MAX(attempt-1,0) ELSE attempt END,
            recovery_attempt=MAX(recovery_attempt-1,0), assigned_device_id=NULL, lease_token=NULL,
            lease_expires_at=NULL, started_at=NULL, finished_at=NULL, available_at=?, updated_at=?,
            last_error='DEVICE_STOP_RELEASED' WHERE assigned_device_id=? AND status IN ('LEASED','RUNNING')""",
            (now, now, row['device_id']))
        counts = json.loads(row['released_tasks'])
        counts['sqliteTasks'] = result.rowcount
        conn.execute("UPDATE collector_device SET current_task_id=NULL,status='STOPPING',updated_at=? WHERE id=?",
                     (now, row['device_id']))
        conn.execute('UPDATE collector_device_command SET release_complete=1,released_tasks=? WHERE id=?',
                     (json.dumps(counts), command_id))
    return payload(conn.execute('SELECT * FROM collector_device_command WHERE id=?', (command_id,)).fetchone())


def acknowledge(conn, device, command_id, status, error, now, fleet):
    row = active(conn, device['id'], fleet['stopGeneration'])
    if row is None or row['id'] != command_id:
        raise DeviceControlError('COMMAND_SUPERSEDED')
    if not row['release_complete'] or not fleet['releaseComplete']:
        raise DeviceControlError('COMMAND_RELEASE_IN_PROGRESS')
    if row['action'] == 'START' and fleet['stopRequired']:
        raise DeviceControlError('FLEET_STOPPED')
    if row['status'] in {'APPLIED', 'FAILED'}:
        return payload(row)
    with conn:
        conn.execute('UPDATE collector_device_command SET status=?, acknowledged_at=?,error=? WHERE id=?',
                     (status, now, error[:500], row['id']))
        # A start receipt confirms service startup; normal heartbeat describes actual task progress.
        if status == 'APPLIED' and row['action'] == 'STOP':
            conn.execute("UPDATE collector_device SET status='STOPPED',current_task_id=NULL,updated_at=? WHERE id=?",
                         (now, device['id']))
    return payload(latest(conn, device['id']))
