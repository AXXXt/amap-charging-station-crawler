"""Offline regression tests for authenticated per-device commands and isolated task release."""
import json
import os
import unittest
from unittest.mock import patch, MagicMock
from fastapi import HTTPException
from fastapi.testclient import TestClient
import api_server
import collector_monitor
import device_control
import fleet_control
import test_mobile_dispatch as dispatch_tests


class DeviceControlTests(unittest.TestCase):
    add_tasks = dispatch_tests.MobileDispatchTests.add_tasks
    claim = dispatch_tests.MobileDispatchTests.claim
    complete = dispatch_tests.MobileDispatchTests.complete

    def setUp(self):
        dispatch_tests.MobileDispatchTests.setUp(self)
        self.settings = patch.dict(os.environ, {'COLLECTOR_MONITOR_KEY': '123456'})
        self.settings.start()
        self.mysql = patch.object(api_server, '_mysql_configured', return_value=False)
        self.mysql.start()
        self.ids = {}
        for code, package in [('BD-one', 'com.baidu.BaiduMap'), ('AM-two', 'com.autonavi.minimap')]:
            result = api_server.mobile_register(api_server.MobileRegisterRequest(
                activationCode='activate', deviceCode=code, name=code, appVersion='test',
                capabilities={'targetPackage': package, 'remoteDeviceControl': 1, 'fleetStopSupported': True}))
            self.devices[code] = result['deviceToken']
            self.ids[code] = result['device']['id']

    def tearDown(self):
        self.mysql.stop()
        self.settings.stop()
        dispatch_tests.MobileDispatchTests.tearDown(self)

    def auth(self, code):
        return 'Bearer ' + self.devices[code]

    def control(self, code):
        return api_server.mobile_config(self.auth(code))['fleetControl']

    def command(self, code, action='START', request='request-1'):
        return api_server.collector_monitor_device_command(self.ids[code],
            api_server.MonitorDeviceCommandRequest(requestId=request, action=action), '123456')['command']

    def ack(self, code, command, status='APPLIED'):
        return api_server.mobile_device_command_result(api_server.DeviceCommandAckRequest(
            commandId=command['commandId'], status=status), self.auth(code))['command']

    def test_http_routes_require_monitor_password_and_device_auth(self):
        client = TestClient(api_server.app)
        route = '/api/v1/collector-monitor/devices/' + self.ids['BD-one'] + '/command'
        body = {'requestId': 'http', 'action': 'START'}
        for headers in ({}, {'X-Collector-Monitor-Key': 'wrong'}):
            self.assertEqual(401, client.post(route, json=body, headers=headers).status_code)
        result = client.post(route, json=body, headers={'X-Collector-Monitor-Key': '123456'})
        self.assertEqual(200, result.status_code)
        command = result.json()['command']
        ack = {'commandId': command['commandId'], 'status': 'APPLIED'}
        self.assertEqual(401, client.post('/api/v1/devices/me/command-result', json=ack).status_code)
        self.assertEqual(409, client.post('/api/v1/devices/me/command-result', json=ack,
            headers={'Authorization': self.auth('AM-two')}).status_code)
        self.assertEqual(200, client.post('/api/v1/devices/me/command-result', json=ack,
            headers={'Authorization': self.auth('BD-one')}).status_code)

    def test_start_modes_and_real_execution_receipt(self):
        baidu = self.command('BD-one')
        amap = self.command('AM-two', request='request-2')
        self.assertEqual('DEVICE_DISPATCH', baidu['mode'])
        self.assertEqual('PRIORITY_DETAIL', amap['mode'])
        self.assertEqual('QUEUED', baidu['status'])
        result = self.ack('BD-one', baidu)
        self.assertEqual('APPLIED', result['status'])
        self.assertTrue(result['acknowledgedAt'])
        self.assertEqual(result, self.ack('BD-one', baidu))

    def test_stop_releases_only_target_tasks_and_refunds_attempt(self):
        self.add_tasks(4)
        target = self.claim('BD-one')
        other = self.claim('AM-two')
        stopped = self.command('BD-one', 'STOP')
        self.assertEqual(1, stopped['releasedTaskCount'])
        self.assertIsNone(self.claim('BD-one'))
        self.assertFalse(self.control('AM-two')['stopRequired'])
        self.complete('AM-two', other)
        with self.assertRaises(HTTPException):
            self.complete('BD-one', target)
        self.assertEqual(stopped, self.command('BD-one', 'STOP'))
        self.assertEqual('APPLIED', self.ack('BD-one', stopped)['status'])
        self.assertTrue(self.control('BD-one')['stopRequired'])
        self.command('BD-one', 'START', 'resume')
        self.assertIsNotNone(self.claim('BD-one'))

    def test_recovery_refund_does_not_change_other_device_or_completed_result(self):
        self.add_tasks(3)
        conn = api_server.mobile_conn()
        with conn:
            conn.execute("UPDATE scan_task SET status='RUNNING',attempt=3,recovery_attempt=2,assigned_device_id=? WHERE id='task-000'", (self.ids['BD-one'],))
            conn.execute("UPDATE scan_task SET status='RUNNING',attempt=1,assigned_device_id=? WHERE id='task-001'", (self.ids['AM-two'],))
            conn.execute("UPDATE scan_task SET status='COMPLETED',result_summary='saved',assigned_device_id=? WHERE id='task-002'", (self.ids['BD-one'],))
        conn.close()
        self.command('BD-one', 'STOP')
        conn = api_server.mobile_conn()
        rows = {r['id']: dict(r) for r in conn.execute('SELECT * FROM scan_task')}
        conn.close()
        self.assertEqual(('FAILED', 3, 1), tuple(rows['task-000'][k] for k in ('status', 'attempt', 'recovery_attempt')))
        self.assertEqual('RUNNING', rows['task-001']['status'])
        self.assertEqual(('COMPLETED', 'saved'), tuple(rows['task-002'][k] for k in ('status', 'result_summary')))

    def test_release_barrier_survives_restart_and_same_request_finishes(self):
        self.add_tasks(1)
        self.claim('BD-one')
        conn = api_server.mobile_conn()
        device = conn.execute('SELECT * FROM collector_device WHERE id=?', (self.ids['BD-one'],)).fetchone()
        pending = device_control.begin(conn, device, 'BAIDU', 'STOP', 'partial', 0, api_server._mobile_utc())
        command_id = pending['id']
        conn.close()
        api_server.init_mobile_db()
        self.assertFalse(self.control('BD-one')['releaseComplete'])
        self.assertFalse(self.control('AM-two')['stopRequired'])
        self.assertIsNone(self.claim('BD-one'))
        with self.assertRaises(HTTPException):
            self.command('BD-one', 'START', 'too-early')
        with self.assertRaises(HTTPException):
            self.ack('BD-one', {'commandId': command_id})
        done = self.command('BD-one', 'STOP', 'partial')
        self.assertTrue(done['releaseComplete'])
        self.assertEqual(1, done['releasedTaskCount'])

    def test_all_stop_supersedes_start_and_new_start_only_resumes_one(self):
        old = self.command('BD-one')
        stopped = api_server.collector_monitor_stop_all(api_server.FleetStopRequest(requestId='all-stop'), '123456')
        self.assertIsNone(self.control('BD-one')['deviceCommand'])
        with self.assertRaises(HTTPException):
            self.ack('BD-one', old)
        self.assertEqual('CANCELLED', self.command('BD-one')['status'])
        self.assertTrue(self.control('BD-one')['stopRequired'])
        self.command('BD-one', request='after-all-stop')
        self.assertFalse(self.control('BD-one')['stopRequired'])
        self.assertTrue(self.control('AM-two')['stopRequired'])
        self.assertEqual(stopped['stopGeneration'], self.control('BD-one')['stopGeneration'])

    def test_manual_resume_cancels_individual_command_and_old_ack(self):
        stop = self.command('BD-one', 'STOP')
        api_server.mobile_resume_device(api_server.FleetGenerationRequest(stopGeneration=0), self.auth('BD-one'))
        self.assertFalse(self.control('BD-one')['stopRequired'])
        with self.assertRaises(HTTPException): self.ack('BD-one', stop)

    def test_old_global_ack_does_not_ack_new_device_stop(self):
        generation = api_server.collector_monitor_stop_all(api_server.FleetStopRequest(requestId='fleet'), '123456')['stopGeneration']
        api_server.mobile_acknowledge_stop(api_server.FleetGenerationRequest(stopGeneration=generation), self.auth('BD-one'))
        self.command('BD-one', request='start-new')
        stop = self.command('BD-one', 'STOP', 'device-stop')
        conn = api_server.mobile_conn()
        operations = collector_monitor.read_operations(conn)
        conn.close()
        device = next(d for d in operations['devices'] if d['id'] == self.ids['BD-one'])
        self.assertFalse(device['stopAcknowledged'])
        self.ack('BD-one', stop)
        conn = api_server.mobile_conn()
        device = next(d for d in collector_monitor.read_operations(conn)['devices'] if d['id'] == self.ids['BD-one'])
        conn.close()
        self.assertTrue(device['stopAcknowledged'])

    def test_request_id_conflict_and_old_app_are_rejected(self):
        self.command('BD-one')
        with self.assertRaises(HTTPException): self.command('AM-two')
        with self.assertRaises(HTTPException): self.command('BD-one', 'STOP')
        conn = api_server.mobile_conn()
        with conn: conn.execute("UPDATE collector_device SET capabilities='{}' WHERE id=?", (self.ids['AM-two'],))
        conn.close()
        with self.assertRaises(HTTPException) as error: self.command('AM-two', request='old-app')
        self.assertEqual('DEVICE_APP_UPGRADE_REQUIRED', error.exception.detail)

    def test_superseded_start_cannot_receive_ack_or_unstop(self):
        old = self.command('BD-one')
        stop = self.command('BD-one', 'STOP', 'new-stop')
        with self.assertRaises(HTTPException): self.ack('BD-one', old)
        self.assertEqual('CANCELLED', self.command('BD-one')['status'])
        self.assertTrue(self.control('BD-one')['stopRequired'])
        self.ack('BD-one', stop, 'FAILED')
        self.assertTrue(self.control('BD-one')['stopRequired'])

    def test_mysql_release_filters_every_task_table(self):
        conn = MagicMock()
        cursor = conn.cursor.return_value.__enter__.return_value
        cursor.fetchone.return_value = {'exists': 1}
        cursor.rowcount = 2
        counts = device_control.release_mysql_leases(conn, self.ids['BD-one'], '2026-11-25 12:00:00')
        updates = [(args[0], args[1]) for args, _ in cursor.execute.call_args_list if args[0].startswith('UPDATE ')]
        self.assertEqual(3, len(updates))
        self.assertEqual(6, sum(counts.values()))
        for sql, params in updates:
            self.assertIn('AND lease_device_id=%s', sql)
            self.assertEqual(self.ids['BD-one'], params[-1])
        conn.commit.assert_called_once()


if __name__ == '__main__': unittest.main()
