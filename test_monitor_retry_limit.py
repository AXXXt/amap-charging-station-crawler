import unittest
from unittest.mock import MagicMock, patch

import monitor_scheduler as scheduler


class MonitorRetryLimitTests(unittest.TestCase):
    def test_legacy_tasks_keep_attempts_and_active_leases(self):
        cursor = MagicMock()
        cursor.fetchall.side_effect = [
            [{"id": 11, "current_round": 5}],
            [
                {"id": 1, "round_id": 50, "status": "PENDING", "attempt": 1, "max_attempts": 3},
                {"id": 2, "round_id": 50, "status": "PENDING", "attempt": 2, "max_attempts": 3},
                {"id": 3, "round_id": 50, "status": "RUNNING", "attempt": 2, "max_attempts": 3},
                {"id": 4, "round_id": 50, "status": "LEASED", "attempt": 3, "max_attempts": 3},
            ],
        ]
        result = scheduler._apply_retry_limit(cursor, "2026-11-04 00:00:00")
        self.assertEqual({"updated": 4, "failed": 1}, result)
        updates = [(args[0], args[1]) for args, _ in cursor.execute.call_args_list
                   if args[0].startswith("UPDATE station_monitor_task")]
        self.assertEqual([1, 2, 3, 4], [params[-1] for _, params in updates])
        self.assertEqual([2], [params[-1] for sql, params in updates if "status='FAILED'" in sql])
        for sql, _ in updates:
            self.assertNotIn("attempt=", sql.replace("max_attempts=", ""))
            self.assertNotIn("lease_", sql)
        round_update = cursor.execute.call_args_list[-1].args
        self.assertIn("failed_count=failed_count+", round_update[0])
        self.assertEqual((1, "2026-11-04 00:00:00", 50), round_update[1])

    def test_migration_is_noop_after_limits_are_applied(self):
        cursor = MagicMock()
        cursor.fetchall.side_effect = [[{"id": 11, "current_round": 5}], []]
        self.assertEqual({"updated": 0, "failed": 0}, scheduler._apply_retry_limit(cursor, "now"))
        self.assertFalse(any(call.args[0].startswith("UPDATE") for call in cursor.execute.call_args_list))

    def test_empty_batch_filter_does_not_touch_database(self):
        cursor = MagicMock()
        self.assertEqual({"updated": 0, "failed": 0}, scheduler._apply_retry_limit(cursor, "now", []))
        cursor.execute.assert_not_called()

    def run_completion(self, attempt, history=True, retried=False):
        row = {"local_task_id": "test-task", "source_key": "source", "batch_id": 11,
               "round_id": 50, "status": "RUNNING", "attempt": attempt, "max_attempts": 2,
               "progress": {"_noMatchRetryScheduled": True} if retried else {}}
        connection = MagicMock()
        with patch.object(scheduler, "_get_row", return_value=row), \
             patch.object(scheduler, "_assert_lease"), \
             patch.object(scheduler, "_has_historical_data", return_value=history), \
             patch.object(scheduler, "_conn", return_value=connection), \
             patch.object(scheduler, "_advance_round") as advance, \
             patch.object(scheduler, "task_payload", side_effect=lambda item: item):
            result = scheduler.complete({}, "test-task", "device", "lease", {"skipReason": "NO_MATCH"})
        return result, advance

    def test_first_no_match_with_history_gets_one_retry(self):
        result, advance = self.run_completion(1)
        self.assertEqual("PENDING", result["status"])
        self.assertEqual(1, result["attempt"])
        advance.assert_not_called()

    def test_second_attempt_cannot_gain_extra_no_match_retry(self):
        # The first attempt might have failed before setting the NO_MATCH marker.
        result, advance = self.run_completion(2)
        self.assertEqual("COMPLETED", result["status"])
        self.assertEqual(2, result["attempt"])
        advance.assert_called_once_with({}, 11)

    def test_no_history_or_previous_no_match_retry_does_not_requeue(self):
        for history, retried in [(False, False), (True, True)]:
            with self.subTest(history=history, retried=retried):
                result, advance = self.run_completion(1, history, retried)
                self.assertEqual("COMPLETED", result["status"])
                advance.assert_called_once_with({}, 11)

    def test_retryable_failure_is_terminal_after_second_attempt(self):
        for attempt, expected in [(1, "PENDING"), (2, "FAILED")]:
            with self.subTest(attempt=attempt):
                row = {"attempt": attempt, "max_attempts": 2, "batch_id": 11, "round_id": 50}
                with patch.object(scheduler, "_get_row", return_value=row), \
                     patch.object(scheduler, "_assert_lease"), \
                     patch.object(scheduler, "_conn", return_value=MagicMock()), \
                     patch.object(scheduler, "_advance_round") as advance, \
                     patch.object(scheduler, "task_payload", side_effect=lambda item: item):
                    result = scheduler.fail({}, "task", "device", "lease", "ERROR", "failure", True)
                self.assertEqual(expected, result["status"])
                self.assertEqual(expected == "FAILED", advance.called)


if __name__ == "__main__":
    unittest.main()
