"""Offline stateful tests of primary retry, one-shot transfer and round restore.

SQLite executes the scheduler transitions; no production connection is used.
The adapter only translates MySQL placeholders/locking/functions, not behavior.
"""
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import cross_platform_monitor as extra
import monitor_scheduler as scheduler


class Cursor:
    def __init__(self, conn):
        self.raw = conn.cursor()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.raw.close()

    def execute(self, sql, args=()):
        sql = sql.replace('%s', '?').replace(' FOR UPDATE SKIP LOCKED', '').replace(' FOR UPDATE', '')
        sql = sql.replace('INSERT IGNORE', 'INSERT OR IGNORE')
        self.raw.execute(sql, args)
        return self

    def fetchone(self):
        row = self.raw.fetchone()
        return dict(row) if row else None

    def fetchall(self):
        return [dict(row) for row in self.raw.fetchall()]

    @property
    def rowcount(self):
        return self.raw.rowcount

    @property
    def lastrowid(self):
        return self.raw.lastrowid


class Connection:
    def __init__(self, path):
        self.raw = sqlite3.connect(path)
        self.raw.row_factory = sqlite3.Row
        self.raw.create_function('CONCAT', -1, lambda *args: ''.join(map(str, args)))
        self.raw.create_function('SHA2', 2, lambda text, bits: hashlib.sha256(text.encode()).hexdigest())
        self.raw.create_function('UUID', 0, lambda: str(uuid.uuid4()))

    def cursor(self):
        return Cursor(self.raw)

    def begin(self):
        self.raw.execute('BEGIN')

    def commit(self):
        self.raw.commit()

    def rollback(self):
        self.raw.rollback()

    def close(self):
        self.raw.close()


SCHEMA = """
CREATE TABLE station_monitor_batch (
 id INTEGER PRIMARY KEY, status TEXT, scope_key TEXT, current_round INTEGER,
 target_rounds INTEGER DEFAULT 0, target_interval_minutes INTEGER DEFAULT 0,
 collection_mode TEXT DEFAULT 'DETAIL', started_at TEXT, finished_at TEXT, updated_at TEXT
);
CREATE TABLE station_monitor_round (
 id INTEGER PRIMARY KEY, batch_id INTEGER, round_no INTEGER, status TEXT,
 completed_count INTEGER DEFAULT 0, failed_count INTEGER DEFAULT 0,
 planned_at TEXT, started_at TEXT, created_at TEXT, updated_at TEXT, finished_at TEXT
);
CREATE TABLE station_monitor_task (
 id INTEGER PRIMARY KEY, task_key TEXT, local_task_id TEXT UNIQUE, batch_id INTEGER,
 round_id INTEGER, round_no INTEGER, source_key TEXT, amap_poi_id TEXT,
 station_name TEXT, station_address TEXT, city TEXT, district TEXT, latitude REAL,
 longitude REAL, sequence_no INTEGER, priority INTEGER DEFAULT 80,
 collection_mode TEXT DEFAULT 'DETAIL', type TEXT DEFAULT 'HENAN_POI_DETAIL',
 status TEXT DEFAULT 'PENDING', attempt INTEGER DEFAULT 0, max_attempts INTEGER DEFAULT 2,
 lease_device_id TEXT DEFAULT '', lease_token TEXT DEFAULT '', lease_expires_at TEXT,
 available_at TEXT, started_at TEXT, finished_at TEXT, last_error TEXT DEFAULT '',
 progress TEXT DEFAULT '{}', result_summary TEXT, created_at TEXT, updated_at TEXT
);
CREATE TABLE station_monitor_batch_station (
 id INTEGER PRIMARY KEY, batch_id INTEGER, source_key TEXT, amap_poi_id TEXT,
 station_name TEXT, station_address TEXT, city TEXT, district TEXT, latitude REAL,
 longitude REAL, sequence_no INTEGER
);
CREATE TABLE station_monitor_supplement_task (
 id INTEGER PRIMARY KEY, local_task_id TEXT UNIQUE, origin_task_id TEXT UNIQUE,
 origin_batch_id INTEGER, origin_round_id INTEGER, target_scope_key TEXT, reason TEXT,
 status INTEGER DEFAULT 0, attempt INTEGER DEFAULT 0, max_attempts INTEGER DEFAULT 1,
 lease_device_id TEXT DEFAULT '', lease_token TEXT DEFAULT '', lease_expires_at INTEGER DEFAULT 0,
 available_at INTEGER, started_at INTEGER DEFAULT 0, finished_at INTEGER DEFAULT 0,
 deadline_at INTEGER, last_error TEXT DEFAULT '', progress TEXT DEFAULT '{}',
 result_summary TEXT DEFAULT '{}', created_at INTEGER, updated_at INTEGER
);
CREATE TABLE site_exploration_charging_station_dynamic_history (
 id INTEGER PRIMARY KEY, task_id TEXT, source_key TEXT, current_price TEXT,
 pile_total INTEGER, pile_idle INTEGER, pile_busy INTEGER, pile_unknown INTEGER, price_json TEXT
);
"""


class CrossPlatformTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = str(Path(self.tmp.name) / 'scheduler.sqlite')
        self.now = (datetime.now(timezone.utc) + timedelta(minutes=1)).strftime('%Y-%m-%d %H:%M:%S')
        conn = sqlite3.connect(self.path)
        conn.executescript(SCHEMA)
        conn.close()
        for name, replacement in (
            ('_conn', lambda config: Connection(self.path)),
            ('init_tables', lambda config: None),
            ('_now', lambda: self.now),
            ('_ensure_active_batch', lambda config, scope: scheduler._active_batch_id(config, scope)),
        ):
            handle = patch.object(scheduler, name, replacement)
            handle.start()
            self.addCleanup(handle.stop)
        # Any accidental unpatched production connection fails immediately.
        network = patch.object(scheduler.pymysql, 'connect', side_effect=AssertionError('network forbidden'))
        network.start()
        self.addCleanup(network.stop)
        self.seed()

    def seed(self):
        for batch, scope in ((10, 'BAIDU_MIN240_499'), (11, 'AMAP_MIN240_647')):
            self.sql('INSERT INTO station_monitor_batch(id,status,scope_key,current_round) VALUES(?,?,?,1)', (batch, 'ACTIVE', scope))
            self.sql("INSERT INTO station_monitor_round(id,batch_id,round_no,status,started_at) VALUES(?,?,1,'RUNNING',?)", (batch*10, batch, self.now))
            self.sql('INSERT INTO station_monitor_batch_station(batch_id,source_key,amap_poi_id,station_name,station_address,city,district,sequence_no) VALUES(?,?,?,?,?,?,?,1)',
                     (batch, f'source-{batch}', f'poi-{batch}', f'Station {batch}', f'Address {batch}', '郑州', '中原'))
            self.sql("INSERT INTO station_monitor_task(local_task_id,batch_id,round_id,round_no,source_key,amap_poi_id,station_name,station_address,city,district,sequence_no,status,attempt,lease_device_id,lease_token,lease_expires_at,available_at) "
                     "VALUES(?,?,?,1,?,?,?,?,?,?,1,'RUNNING',1,'source-device','lease','2099-01-01 00:00:00',?)",
                     (f'task-{batch}', batch, batch*10, f'source-{batch}', f'poi-{batch}', f'Station {batch}', f'Address {batch}', '郑州', '中原', self.now))

    def sql(self, sql, args=()):
        conn = sqlite3.connect(self.path)
        try:
            with conn:
                conn.row_factory = sqlite3.Row
                result = conn.execute(sql, args)
                return [dict(row) for row in result.fetchall()] if result.description else result.rowcount
        finally:
            conn.close()

    def task(self, batch=11):
        return self.sql('SELECT * FROM station_monitor_task WHERE local_task_id=?', (f'task-{batch}',))[0]

    def supplement(self):
        return self.sql('SELECT * FROM station_monitor_supplement_task')[0]

    def tick(self, seconds=11):
        self.now = (scheduler._as_utc(self.now) + timedelta(seconds=seconds)).strftime('%Y-%m-%d %H:%M:%S')

    def complete(self, batch=11, summary=None, lease='lease', device='source-device'):
        return scheduler.complete({}, f'task-{batch}', device, lease, summary or {})

    def snapshot(self, task='task-11', source='source-11', price='0.80', total=10, idle=3, busy=7, unknown=0, tariff=None):
        self.sql('INSERT INTO site_exploration_charging_station_dynamic_history(task_id,source_key,current_price,pile_total,pile_idle,pile_busy,pile_unknown,price_json) VALUES(?,?,?,?,?,?,?,?)',
                 (task, source, price, total, idle, busy, unknown, json.dumps(tariff) if tariff is not None else None))

    def exhausted(self, batch=11, summary=None):
        self.sql('UPDATE station_monitor_task SET attempt=2 WHERE local_task_id=?', (f'task-{batch}',))
        return self.complete(batch, summary)

    def claim_extra(self, batch=11):
        target = extra.SCOPES['AMAP_MIN240_647' if batch == 11 else 'BAIDU_MIN240_499']
        payload, reason = scheduler.claim({}, {'id': 'target-device'}, target)
        self.assertEqual('CROSS_PLATFORM_CLAIMED', reason)
        self.assertEqual(1, payload['attempt'])
        self.assertEqual(1, payload['maxAttempts'])
        self.assertEqual(0, payload['maxRecoveryAttempts'])
        return payload

    def test_no_match_retries_once_without_historical_data_then_transfers_both_directions(self):
        for batch in (10, 11):
            first = self.complete(batch, {'skipReason': 'NO_MATCH'})
            self.assertEqual('PENDING', self.task(batch)['status'])
            self.assertFalse(self.sql('SELECT * FROM station_monitor_supplement_task WHERE origin_task_id=?', (f'task-{batch}',)))
            self.tick()
            scope = 'BAIDU_MIN240_499' if batch == 10 else 'AMAP_MIN240_647'
            second, reason = scheduler.claim({}, {'id': 'source-device'}, scope)
            self.assertEqual(2, second['attempt'])
            self.complete(batch, {'skipReason': 'NO_MATCH'}, second['leaseToken'])
            payload = self.claim_extra(batch)
            self.assertEqual(extra.SCOPES[scope], payload['crossPlatform']['targetScopeKey'])
            self.assertEqual(f'poi-{batch}', payload['stationId'])
            self.assertEqual(f'Address {batch}', payload['stationAddress'])
            self.assertNotEqual(f'task-{batch}', payload['id'])
            scheduler.complete({}, payload['id'], 'target-device', payload['leaseToken'], {'skipReason': 'NO_MATCH'})

    def test_missing_card_error_is_retried_even_if_client_says_non_retryable(self):
        first = scheduler.fail({}, 'task-11', 'source-device', 'lease', 'COLLECTION_FAILED', '详情首页数据不完整（实时电价）', False)
        self.assertEqual(1, first['attempt'])
        self.assertEqual('PENDING', self.task()['status'])
        self.tick()
        payload, _ = scheduler.claim({}, {'id': 'source-device'}, 'AMAP_MIN240_647')
        scheduler.fail({}, 'task-11', 'source-device', payload['leaseToken'], 'MISSING_PILE_STATUS', 'no piles', False)
        self.assertEqual('MISSING_STATION_DATA', self.supplement()['reason'])

    def test_completed_or_skipped_without_result_gets_original_retry_then_one_supplement(self):
        self.complete(summary={'collected': 1})
        self.assertEqual('PENDING', self.task()['status'])
        self.tick()
        payload, _ = scheduler.claim({}, {'id': 'source-device'}, 'AMAP_MIN240_647')
        self.complete(summary={'skipped': True}, lease=payload['leaseToken'])
        self.assertEqual('NO_VALID_RESULT', self.supplement()['reason'])

    def test_partial_or_invalid_snapshots_do_not_count_as_effective_result(self):
        cases = [dict(price=''), dict(total=0, idle=0, busy=0), dict(idle=11), dict(unknown=1), dict(price='NaN'), dict(idle=-1), dict(busy=0)]
        for fields in cases:
            with self.subTest(fields=fields):
                self.sql('DELETE FROM site_exploration_charging_station_dynamic_history')
                self.snapshot(**fields)
                conn = scheduler._conn({})
                with conn.cursor() as cursor:
                    self.assertFalse(scheduler._has_valid_task_result(cursor, self.task()))
                conn.close()

    def test_valid_zero_price_and_zero_idle_are_accepted_without_tou_record(self):
        self.snapshot(price='0', idle=0, busy=10)
        self.complete()
        self.assertEqual('COMPLETED', self.task()['status'])
        self.assertFalse(self.sql('SELECT * FROM station_monitor_supplement_task'))
        self.assertEqual(2, self.sql('SELECT current_round FROM station_monitor_batch WHERE id=11')[0]['current_round'])

    @staticmethod
    def tariff(platform="AMAP"):
        return {"priceSchedulePlatform": platform,
                "fastPrices": [{"time": "00:00-16:00", "totalPrice": "0.77", "elecFee": "0.57", "serviceFee": "0.20"},
                               {"time": "16:00-24:00", "totalPrice": "1.33", "elecFee": "1.13", "serviceFee": "0.20"}]}

    def test_amap_tariff_without_live_counts_is_kept_without_retry_or_transfer(self):
        self.snapshot(price='', total=0, idle=0, busy=0, tariff=self.tariff())
        self.complete(summary={'collected': 1})
        self.assertEqual('COMPLETED', self.task()['status'])
        self.assertFalse(self.sql('SELECT * FROM station_monitor_supplement_task'))
        history = self.sql('SELECT * FROM site_exploration_charging_station_dynamic_history')[0]
        self.assertEqual(0, history['pile_total'])
        self.assertEqual('', history['current_price'])
        self.assertEqual(self.tariff(), json.loads(history['price_json']))

    def test_amap_price_only_does_not_relax_baidu_primary_validation(self):
        self.snapshot(task='task-10', source='source-10', total=0, idle=0, busy=0, tariff=self.tariff())
        self.complete(batch=10)
        self.assertEqual('PENDING', self.task(10)['status'])

    def test_baidu_to_amap_supplement_accepts_tariff_without_live_counts(self):
        self.exhausted(batch=10)
        payload = self.claim_extra(batch=10)
        self.snapshot(task=payload['id'], source='source-10', total=0, idle=0, busy=0, tariff=self.tariff())
        scheduler.complete({}, payload['id'], 'target-device', payload['leaseToken'], {'collected': 1})
        self.assertEqual(extra.COMPLETED, self.supplement()['status'])

    def test_amap_to_baidu_supplement_still_requires_live_counts(self):
        self.exhausted()
        payload = self.claim_extra()
        self.snapshot(task=payload['id'], total=0, idle=0, busy=0, tariff=self.tariff())
        scheduler.complete({}, payload['id'], 'target-device', payload['leaseToken'], {'collected': 1})
        self.assertEqual(extra.FAILED, self.supplement()['status'])

    def test_amap_tariff_from_other_task_or_station_is_not_accepted(self):
        self.snapshot(task='old-task', total=0, idle=0, busy=0, tariff=self.tariff())
        self.snapshot(source='another-station', total=0, idle=0, busy=0, tariff=self.tariff())
        self.complete()
        self.assertEqual('PENDING', self.task()['status'])

    def test_empty_malformed_or_wrong_platform_tariffs_are_not_accepted(self):
        cases = [{}, {'priceSchedulePlatform': 'AMAP', 'fastPrices': [{}]}, self.tariff('BAIDU'),
                 {'priceSchedulePlatform': 'AMAP', 'fastPrices': [{'time': '当前时段', 'totalPrice': '0.77'}]},
                 {'priceSchedulePlatform': 'AMAP', 'fastPrices': [{'time': '00:00-24:01', 'totalPrice': '0.77'}]},
                 {'priceSchedulePlatform': 'AMAP', 'fastPrices': [{'time': '00:00-24:00', 'totalPrice': 'NaN'}]}]
        for tariff in cases:
            with self.subTest(tariff=tariff):
                self.sql('DELETE FROM site_exploration_charging_station_dynamic_history')
                self.snapshot(total=0, idle=0, busy=0, tariff=tariff)
                conn = scheduler._conn({})
                try:
                    with conn.cursor() as cursor:
                        self.assertFalse(scheduler._has_valid_task_result(cursor, self.task()))
                finally:
                    conn.close()

    def test_other_round_or_other_station_data_cannot_satisfy_current_task(self):
        self.snapshot(task='old-task')
        self.snapshot(source='another-station')
        self.exhausted()
        self.assertEqual('NO_VALID_RESULT', self.supplement()['reason'])

    def test_reported_failure_after_valid_upload_is_completed_without_transfer(self):
        self.snapshot()
        scheduler.fail({}, 'task-11', 'source-device', 'lease', 'COLLECTION_FAILED', 'late client error', True)
        self.assertEqual('COMPLETED', self.task()['status'])
        self.assertFalse(self.sql('SELECT * FROM station_monitor_supplement_task'))

    def test_infrastructure_failure_does_not_transfer(self):
        self.sql("UPDATE station_monitor_task SET attempt=2 WHERE local_task_id='task-11'")
        scheduler.fail({}, 'task-11', 'source-device', 'lease', 'UPLOAD_FAILED', 'network unavailable', True)
        self.assertEqual('FAILED', self.task()['status'])
        self.assertFalse(self.sql('SELECT * FROM station_monitor_supplement_task'))

    def test_transfer_waits_for_supplement_and_restores_original_formal_membership(self):
        self.exhausted()
        self.assertEqual(1, scheduler._start_next_round({}, 11))
        payload = self.claim_extra()
        scheduler.ack({}, payload['id'], 'target-device', payload['leaseToken'])
        scheduler.progress({}, payload['id'], 'target-device', payload['leaseToken'], {'step': 'DETAIL'})
        self.snapshot(task=payload['id'])
        scheduler.complete({}, payload['id'], 'target-device', payload['leaseToken'], {'collected': 1})
        self.assertEqual(extra.COMPLETED, self.supplement()['status'])
        next_tasks = self.sql('SELECT batch_id,source_key,attempt FROM station_monitor_task WHERE round_no=2')
        self.assertEqual([{'batch_id': 11, 'source_key': 'source-11', 'attempt': 0}], next_tasks)
        self.assertEqual(2, len(self.sql('SELECT * FROM station_monitor_batch_station')))
        self.assertEqual('source-10', self.sql('SELECT source_key FROM station_monitor_batch_station WHERE batch_id=10')[0]['source_key'])

    def test_supplement_failure_is_final_even_retryable_and_cannot_transfer_back(self):
        self.exhausted()
        payload = self.claim_extra()
        scheduler.fail({}, payload['id'], 'target-device', payload['leaseToken'], 'NO_MATCH', 'not found', True)
        self.assertEqual(extra.FAILED, self.supplement()['status'])
        self.assertEqual(1, self.supplement()['attempt'])
        self.assertEqual(1, len(self.sql('SELECT * FROM station_monitor_supplement_task')))
        self.assertEqual(2, self.sql('SELECT current_round FROM station_monitor_batch WHERE id=11')[0]['current_round'])
        with self.assertRaises(scheduler.MonitorTaskError):
            scheduler.complete({}, payload['id'], 'target-device', payload['leaseToken'], {})

    def test_supplement_has_separate_upload_identity_but_same_canonical_station(self):
        self.exhausted()
        payload = self.claim_extra()
        source_task = scheduler.get_task({}, payload['id'])
        original_task = scheduler.get_task({}, 'task-11')
        self.assertEqual(original_task['source_station_id'], source_task['source_station_id'])
        self.assertNotEqual(original_task['id'], source_task['id'])
        self.assertEqual('target-device', source_task['assigned_device_id'])
        self.assertEqual('task-11', source_task['cross_platform']['originTaskId'])

    def test_duplicate_primary_callback_and_enqueue_cannot_duplicate_supplement_or_counter(self):
        self.exhausted()
        with self.assertRaises(scheduler.MonitorTaskError):
            self.complete()
        conn = scheduler._conn({})
        with conn.cursor() as cursor:
            extra.enqueue(cursor, self.task(), 'NO_VALID_RESULT', self.now)
        conn.commit()
        conn.close()
        self.assertEqual(1, len(self.sql('SELECT * FROM station_monitor_supplement_task')))
        self.assertEqual(1, self.sql('SELECT completed_count FROM station_monitor_round WHERE id=110')[0]['completed_count'])

    def test_wrong_device_cannot_finish_primary_or_supplement(self):
        with self.assertRaises(scheduler.MonitorTaskError):
            self.complete(device='wrong')
        self.assertEqual('RUNNING', self.task()['status'])
        self.exhausted()
        payload = self.claim_extra()
        with self.assertRaises(scheduler.MonitorTaskError):
            scheduler.complete({}, payload['id'], 'wrong', payload['leaseToken'], {})
        self.assertEqual(extra.LEASED, self.supplement()['status'])

    def test_unclaimed_supplement_remains_pending_until_target_platform_is_available(self):
        self.exhausted()
        self.tick(3600)
        self.assertEqual(1, scheduler._start_next_round({}, 11))
        self.assertEqual(extra.PENDING, self.supplement()['status'])
        self.assertEqual(0, self.supplement()['attempt'])
        self.claim_extra()

    def test_expired_supplement_lease_is_never_requeued(self):
        self.exhausted()
        payload = self.claim_extra()
        self.tick(extra.LEASE_SECONDS + 1)
        self.assertEqual(2, scheduler._start_next_round({}, 11))
        self.assertEqual(extra.FAILED, self.supplement()['status'])
        self.assertEqual(1, self.supplement()['attempt'])
        self.assertEqual('LEASE_EXPIRED', self.supplement()['last_error'])

    def test_failed_validation_read_rolls_back_without_transfer_or_terminal_state(self):
        with patch.object(scheduler, '_has_valid_task_result', side_effect=RuntimeError('db unavailable')):
            with self.assertRaises(RuntimeError):
                self.complete()
        self.assertEqual('RUNNING', self.task()['status'])
        self.assertFalse(self.sql('SELECT * FROM station_monitor_supplement_task'))

    def test_supplement_is_not_claimed_by_its_original_platform(self):
        self.exhausted()
        payload, _ = scheduler.claim({}, {'id': 'source-device'}, 'AMAP_MIN240_647')
        self.assertIsNone(payload)
        self.assertEqual(0, self.supplement()['attempt'])


class CrossPlatformApiTests(unittest.TestCase):
    def test_claim_uses_target_platform_tariff_cache(self):
        import api_server
        for target in extra.SCOPES:
            payload = {'id': 'XPL-test', 'crossPlatform': {'targetScopeKey': target}}
            mode = 'MONITOR_' + target
            with patch.object(api_server.monitor_scheduler, 'claim', return_value=(payload, 'CROSS_PLATFORM_CLAIMED')), \
                 patch.object(api_server, '_with_daily_tariff', side_effect=lambda task, platform: dict(task, cachePlatform=platform)) as cache, \
                 patch.object(api_server, '_update_monitor_device_task'):
                result = api_server._mobile_claim_allowed(api_server.MobileClaimRequest(mode=mode, deviceCode='test'), {'id': 'device'})
            self.assertEqual(target.split('_')[0], result['task']['cachePlatform'])
            cache.assert_called_once_with(payload, target.split('_')[0])

    def test_stop_never_refunds_or_requeues_supplemental_attempt(self):
        import fleet_control
        conn = MagicMock()
        cursor = conn.cursor.return_value.__enter__.return_value
        cursor.fetchone.return_value = {'exists': 1}
        cursor.rowcount = 1
        result = fleet_control.release_mysql_leases(conn, '2026-11-13 00:00:00')
        updates = [call.args for call in cursor.execute.call_args_list if call.args[0].startswith('UPDATE station_monitor_supplement_task')]
        self.assertEqual(1, len(updates))
        self.assertIn('status=4', updates[0][0])
        self.assertNotIn('attempt=', updates[0][0])
        self.assertNotIn('PENDING', updates[0][0])
        self.assertEqual(1, result[extra.TABLE])
        conn.commit.assert_called_once()


if __name__ == '__main__':
    unittest.main()
