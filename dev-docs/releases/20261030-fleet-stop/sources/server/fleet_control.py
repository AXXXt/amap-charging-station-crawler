"""Persistent fleet stop commands and dispatch serialization for API workers.

Stopping invalidates every device's permission to claim. Only an explicit
manual resume for the current generation restores that device's permission.
"""
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import os
import sqlite3


class FleetControlError(Exception):
    pass


def init_schema(conn):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS fleet_control (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            generation INTEGER NOT NULL DEFAULT 0,
            request_id TEXT NOT NULL DEFAULT '',
            requested_by TEXT NOT NULL DEFAULT '',
            requested_at TEXT NOT NULL DEFAULT '',
            release_complete INTEGER NOT NULL DEFAULT 1,
            released_tasks TEXT NOT NULL DEFAULT '{}'
        );
        INSERT OR IGNORE INTO fleet_control(id) VALUES(1);
        CREATE TABLE IF NOT EXISTS fleet_device_control (
            device_id TEXT PRIMARY KEY,
            allowed_generation INTEGER NOT NULL DEFAULT 0,
            stopped_generation INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS fleet_stop_request (
            request_id TEXT PRIMARY KEY,
            generation INTEGER NOT NULL,
            response TEXT NOT NULL DEFAULT ''
        );
    """)


@contextmanager
def dispatch_guard(mobile_db_path):
    # A separate file avoids deadlocking the normal mobile DB connections.
    # SQLite locking covers every uvicorn worker on this deployment host.
    guard = sqlite3.connect(str(mobile_db_path) + '.dispatch-lock', timeout=30)
    try:
        guard.execute('BEGIN IMMEDIATE')
        yield
    finally:
        guard.rollback()
        guard.close()


def can_control(device):
    codes = {s.strip() for s in os.getenv('MOBILE_FLEET_CONTROLLER_DEVICE_CODES', '').split(',') if s.strip()}
    if codes:
        return device['device_code'] in codes
    # Default controller is the user's Baidu collector on Redmi K60.
    return (device['device_code'].startswith('BD-')
            and device['name'].strip().lower() in {'23013rk75c', 'redmi k60', 'k60'})


def snapshot(conn, device):
    state = conn.execute('SELECT * FROM fleet_control WHERE id=1').fetchone()
    row = conn.execute('SELECT * FROM fleet_device_control WHERE device_id=?', (device['id'],)).fetchone()
    allowed = row['allowed_generation'] if row else 0
    stopped = row['stopped_generation'] if row else 0
    return {
        'supported': True,
        'stopGeneration': state['generation'],
        'stopRequired': allowed < state['generation'],
        'stoppedGeneration': stopped,
        'releaseComplete': bool(state['release_complete']),
        'requestedAt': state['requested_at'],
        'canStopAll': can_control(device),
    }


def begin_stop(conn, request_id, device_id, now):
    """Called with dispatch_guard held. The durable barrier precedes release."""
    existing = conn.execute('SELECT * FROM fleet_stop_request WHERE request_id=?', (request_id,)).fetchone()
    if existing:
        if existing['response']:
            return json.loads(existing['response'])
        state = conn.execute('SELECT * FROM fleet_control WHERE id=1').fetchone()
        if state['request_id'] != request_id:
            raise FleetControlError('STOP_REQUEST_SUPERSEDED')
        return None
    state = conn.execute('SELECT * FROM fleet_control WHERE id=1').fetchone()
    if not state['release_complete']:
        # Retry a partially completed stop rather than abandon its generation.
        raise FleetControlError('STOP_RELEASE_INCOMPLETE_RETRY_PREVIOUS_REQUEST')
    generation = state['generation'] + 1
    with conn:
        conn.execute("UPDATE fleet_control SET generation=?, request_id=?, requested_by=?, requested_at=?, release_complete=0, released_tasks='{}' WHERE id=1",
                     (generation, request_id, device_id, now))
        conn.execute('INSERT INTO fleet_stop_request(request_id,generation) VALUES(?,?)', (request_id,generation))
    return None


def release_mysql_leases(conn, now):
    """Return active tasks to their queue without consuming a retry.

    Table existence is checked for installations using only one queue type.
    Existing observations/results and terminal tasks are untouched.
    """
    counts = {}
    tables = ('station_monitor_task', 'henan_heavy_truck_charging_station_task')
    try:
        with conn.cursor() as cursor:
            for table in tables:
                cursor.execute('SELECT 1 FROM information_schema.tables WHERE table_schema=DATABASE() AND table_name=%s', (table,))
                if not cursor.fetchone():
                    counts[table] = 0
                    continue
                # Recovery tasks must remain on their FAILED recovery queue.
                if table == 'henan_heavy_truck_charging_station_task':
                    cursor.execute(
                        "UPDATE " + table + " SET status=CASE WHEN recovery_attempt>0 THEN 'FAILED' ELSE 'PENDING' END, "
                        "attempt=CASE WHEN recovery_attempt=0 THEN GREATEST(attempt-1,0) ELSE attempt END, "
                        "recovery_attempt=GREATEST(recovery_attempt-1,0), "
                        "lease_device_id='',lease_token='',lease_expires_at=NULL,started_at=NULL,finished_at=NULL, "
                        "available_at=%s,updated_at=%s,last_error='FLEET_STOP_RELEASED' "
                        "WHERE status IN ('LEASED','RUNNING')", (now,now))
                else:
                    cursor.execute(
                        "UPDATE " + table + " SET status='PENDING',attempt=GREATEST(attempt-1,0), "
                        "lease_device_id='',lease_token='',lease_expires_at=NULL,started_at=NULL,finished_at=NULL, "
                        "available_at=%s,updated_at=%s,last_error='FLEET_STOP_RELEASED' "
                        "WHERE status IN ('LEASED','RUNNING')", (now,now))
                counts[table] = cursor.rowcount
        conn.commit()
        return counts
    except Exception:
        conn.rollback()
        raise


def finish_stop(conn, request_id, mysql_counts, now):
    state = conn.execute('SELECT * FROM fleet_control WHERE id=1').fetchone()
    if state['request_id'] != request_id:
        raise FleetControlError('STOP_REQUEST_SUPERSEDED')
    # Persist the MySQL release receipt so retries after a crash do not lose it.
    previous_counts = json.loads(state['released_tasks'] or '{}')
    counts = {key: previous_counts.get(key,0) + value for key,value in mysql_counts.items()}
    with conn:
        conn.execute('UPDATE fleet_control SET released_tasks=? WHERE id=1', (json.dumps(counts),))
        result = conn.execute(
            "UPDATE scan_task SET status=CASE WHEN recovery_attempt>0 THEN 'FAILED' ELSE 'PENDING' END, "
            "attempt=CASE WHEN recovery_attempt=0 THEN MAX(attempt-1,0) ELSE attempt END, "
            "recovery_attempt=MAX(recovery_attempt-1,0),assigned_device_id=NULL,lease_token=NULL, "
            "lease_expires_at=NULL,started_at=NULL,finished_at=NULL,available_at=?,updated_at=?, "
            "last_error='FLEET_STOP_RELEASED' WHERE status IN ('LEASED','RUNNING')", (now,now))
        counts['sqliteTasks'] = result.rowcount
        conn.execute("UPDATE collector_device SET current_task_id=NULL,status='STOPPING',updated_at=?", (now,))
        device_count = conn.execute('SELECT COUNT(*) FROM collector_device').fetchone()[0]
        response = {'status':'STOP_REQUESTED','requestId':request_id,'stopGeneration':state['generation'],
                    'deviceCount':device_count,'releasedTasks':counts,'releasedTaskCount':sum(counts.values())}
        conn.execute('UPDATE fleet_control SET release_complete=1,released_tasks=? WHERE id=1', (json.dumps(counts),))
        conn.execute('UPDATE fleet_stop_request SET response=? WHERE request_id=?', (json.dumps(response),request_id))
    return response


def resume_device(conn, device, generation):
    control = snapshot(conn,device)
    if generation != control['stopGeneration']:
        raise FleetControlError('NEW_STOP_COMMAND_RECEIVED')
    if not control['releaseComplete']:
        raise FleetControlError('STOP_RELEASE_IN_PROGRESS')
    with conn:
        conn.execute('INSERT INTO fleet_device_control(device_id,allowed_generation) VALUES(?,?) '
                     'ON CONFLICT(device_id) DO UPDATE SET allowed_generation=excluded.allowed_generation',
                     (device['id'],generation))
    return snapshot(conn,device)


def acknowledge_stop(conn, device, generation, now):
    control = snapshot(conn,device)
    if generation != control['stopGeneration']:
        raise FleetControlError('NEW_STOP_COMMAND_RECEIVED')
    with conn:
        conn.execute('INSERT INTO fleet_device_control(device_id,stopped_generation) VALUES(?,?) '
                     'ON CONFLICT(device_id) DO UPDATE SET stopped_generation=MAX(stopped_generation,excluded.stopped_generation)',
                     (device['id'],generation))
        # An old acknowledgment must not undo an explicit manual resume.
        if control['stopRequired']:
            conn.execute("UPDATE collector_device SET status='STOPPED',current_task_id=NULL,updated_at=? WHERE id=?", (now,device['id']))
    return snapshot(conn,device)
