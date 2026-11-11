"""Regression coverage for cross-platform station-card summary replacement."""
import copy
import json
import re
import unittest
from unittest.mock import MagicMock, Mock

from site_exploration_bridge import SiteExplorationBridge, _pile_snapshot_summary


class LatestPileSummaryTests(unittest.TestCase):
    def setUp(self):
        self.bridge = SiteExplorationBridge({}, enabled=True)
        self.bridge._result_column_meta = {"captured_at": {"type": "int"}}
        self.conn = MagicMock()
        self.cursor = self.conn.cursor.return_value.__enter__.return_value
        self.bridge.mysql_connect = Mock(return_value=self.conn)
        self.bridge._append_dynamic_snapshot = Mock()
        self.previous = {
            "sourcePage": "AMAP_DETAIL",
            "currentPrice": "0.60",
            "fastPrices": [{"startTime": "00:00", "endTime": "24:00", "price": "0.60"}],
            "priceTrendTitle": "All day",
            "fastAvailable": "16", "fastTotal": "22", "fastPower": "300-400kW",
            "superAvailable": "", "superTotal": "", "superPower": "",
            "slowAvailable": "", "slowTotal": "", "slowPower": "",
            "chargingPileTotal": 22,
            "chargingPileCount": 22,
            "chargingPiles": [
                {"name": str(i), "chargingType": "fast", "power": "300kW", "status": "idle"}
                for i in range(22)
            ],
        }
        self.incoming = {
            "sourcePage": "BAIDU_DETAIL", "pileStatusSource": "DETAIL_CARD",
            "fastAvailable": "", "fastTotal": "", "fastPower": "",
            "superAvailable": "18", "superTotal": "22", "superPower": "300-400kW",
            "slowAvailable": "", "slowTotal": "", "slowPower": "",
            "chargingPiles": [], "chargingPileTotal": 22,
        }

    def existing_row(self):
        self.cursor.fetchone.return_value = {
            "result_payload": json.dumps(self.previous), "current_price": "0.60",
        }

    def merge(self, payload):
        self.existing_row()
        return self.bridge._merge_latest_result_payload(self.conn, "station-key", payload)

    def upsert(self, payload):
        self.existing_row()
        task = {
            "id": "test-task", "type": "HENAN_POI_DETAIL", "keyword": "test station",
            "source_payload": "{}", "source_station_id": "test-poi",
            "province": "", "city": "", "district": "", "search_region": "",
        }
        self.bridge.upsert_result(task, "obs", "test-poi", "device", payload, 200, 201)
        sql, params = self.cursor.execute.call_args.args
        columns = re.search(r"INSERT INTO .*? \((.*?)\) VALUES", sql).group(1).split(", ")
        return sql, dict(zip(columns, params))

    def test_baidu_super_card_replaces_amap_fast_card(self):
        merged = self.merge(self.incoming)
        self.assertEqual(merged["fastTotal"], "")
        self.assertEqual(merged["fastAvailable"], "")
        self.assertEqual(merged["fastPower"], "")
        self.assertEqual(merged["superTotal"], "22")
        self.assertEqual(merged["superAvailable"], "18")
        self.assertEqual(sum(int(merged.get(p + "Total") or 0) for p in ("fast", "super", "slow")), 22)

    def test_amap_fast_card_replaces_baidu_super_card(self):
        self.previous.update(self.incoming)
        incoming = {"fastAvailable": "7", "fastTotal": "22", "fastPower": "300kW"}
        merged = self.merge(incoming)
        self.assertEqual(merged["superTotal"], "")
        self.assertEqual(merged["superAvailable"], "")
        self.assertEqual(merged["fastAvailable"], "7")

    def test_static_piles_and_price_periods_are_retained(self):
        previous = copy.deepcopy(self.previous)
        incoming = copy.deepcopy(self.incoming)
        merged = self.merge(incoming)
        self.assertEqual(merged["chargingPiles"], previous["chargingPiles"])
        self.assertEqual(merged["chargingPileCount"], 22)
        self.assertEqual(merged["currentPrice"], "0.60")
        self.assertEqual(merged["fastPrices"], previous["fastPrices"])
        self.assertEqual(incoming, self.incoming)
        self.assertEqual(self.previous, previous)

    def test_changed_live_total_is_not_replaced_by_static_pile_total(self):
        incoming = dict(self.incoming, superAvailable="8", superTotal="10", chargingPileTotal=10)
        merged = self.merge(incoming)
        self.assertEqual(merged["chargingPileTotal"], 10)
        self.assertEqual(merged["chargingPileCount"], 22)
        self.assertEqual(len(merged["chargingPiles"]), 22)

    def test_zero_idle_and_explicit_zero_total_are_observations(self):
        for idle, total in ((0, 22), ("0", "22"), (0, 0), ("0", "0")):
            with self.subTest(idle=idle, total=total):
                sql, values = self.upsert({"superAvailable": idle, "superTotal": total})
                self.assertEqual(values["fast_total"], "")
                self.assertEqual(values["super_available"], "0")
                self.assertEqual(values["super_total"], str(total))
                self.assertNotIn("IF(VALUES(super_available) <> ''", sql)

    def test_missing_idle_stays_unknown_instead_of_inheriting_previous(self):
        merged = self.merge({"fastTotal": "22"})
        self.assertEqual(merged["fastAvailable"], "")
        live = {key: value for key, value in merged.items() if key != "chargingPiles"}
        _, overall = _pile_snapshot_summary(live)
        self.assertEqual(overall, {"idle": 0, "busy": 0, "unknown": 22, "total": 22})
        sql, values = self.upsert({"fastTotal": "22"})
        self.assertEqual(values["fast_available"], "")
        self.assertNotIn("IF(VALUES(fast_available) <> ''", sql)

    def test_multiple_cards_from_same_observation_are_all_kept(self):
        incoming = {"fastAvailable": "8", "fastTotal": "10", "superAvailable": "3", "superTotal": "5"}
        merged = self.merge(incoming)
        _, overall = _pile_snapshot_summary(incoming)
        self.assertEqual(merged["fastTotal"], "10")
        self.assertEqual(merged["superTotal"], "5")
        self.assertEqual(merged["chargingPileTotal"], 15)
        self.assertEqual(overall["idle"], 11)
        self.assertEqual(overall["total"], 15)

    def test_price_only_upload_keeps_previous_summary(self):
        incoming = {"currentPrice": "0.70", "chargingPiles": [], "chargingPileTotal": 0}
        merged = self.merge(incoming)
        self.assertEqual(merged["fastAvailable"], "16")
        self.assertEqual(merged["fastTotal"], "22")
        self.assertEqual(merged["currentPrice"], "0.70")
        self.assertEqual(merged["chargingPileTotal"], 22)
        sql, values = self.upsert(incoming)
        self.assertEqual(values["fast_available"], "16")
        self.assertIn("IF(VALUES(fast_available) <> ''", sql)

    def test_unrecognised_counts_do_not_clear_unrelated_categories(self):
        for raw in (None, "", "unknown", -1, True):
            with self.subTest(raw=raw):
                merged = self.merge({"superTotal": raw, "currentPrice": "0.70"})
                self.assertEqual(merged["fastTotal"], "22")
                self.assertEqual(merged["fastAvailable"], "16")

    def test_fresh_upsert_clears_missing_scalar_categories_and_keeps_time_guard(self):
        sql, values = self.upsert(self.incoming)
        for prefix in ("fast", "super", "slow"):
            for suffix in ("available", "total", "power"):
                column = prefix + "_" + suffix
                self.assertNotIn("IF(VALUES(" + column + ") <> ''", sql)
                self.assertIn(column + " = IF(VALUES(captured_at) >= captured_at OR captured_at = 0, VALUES(" + column + "), " + column + ")", sql)
        self.assertEqual(values["fast_total"], "")
        self.assertEqual(values["fast_available"], "")
        self.assertEqual(values["super_total"], "22")
        self.assertEqual(values["super_available"], "18")
        self.conn.commit.assert_called_once()

    def test_snapshot_receives_raw_observation_not_retained_old_piles(self):
        self.upsert(self.incoming)
        observed = self.bridge._append_dynamic_snapshot.call_args.kwargs["payload"]
        self.assertIs(observed, self.incoming)
        self.assertEqual(observed["chargingPiles"], [])
        _, overall = _pile_snapshot_summary(observed)
        self.assertEqual(overall["idle"], 18)
        self.assertEqual(overall["total"], 22)

    def test_available_only_card_does_not_inherit_a_previous_total(self):
        merged = self.merge({"fastAvailable": "7"})
        self.assertEqual(merged["fastAvailable"], "7")
        self.assertEqual(merged["fastTotal"], "")
        self.assertEqual(merged["superTotal"], "")

    def test_numeric_zero_card_is_saved_in_dynamic_history(self):
        self.cursor.fetchone.return_value = None
        SiteExplorationBridge._append_dynamic_snapshot(
            self.bridge, self.conn, "key", "poi", "obs", "task", "device",
            "HENAN_POI", "station", {"fastAvailable": 0, "fastTotal": 0}, 200, 201,
        )
        sql, params = self.cursor.execute.call_args.args
        columns = re.search(r"INSERT INTO .*? \((.*?)\) VALUES", sql).group(1).split(", ")
        values = dict(zip(columns, params))
        self.assertEqual(values["fast_total"], 0)
        self.assertEqual(values["pile_idle"], 0)
        self.assertEqual(values["pile_total"], 0)
        self.assertEqual(json.loads(values["pile_json"]), [])

    def test_empty_upload_does_not_create_a_zero_history_snapshot(self):
        SiteExplorationBridge._append_dynamic_snapshot(
            self.bridge, self.conn, "key", "poi", "obs", "task", "device",
            "HENAN_POI", "station", {"chargingPiles": [], "chargingPileTotal": 0}, 200, 201,
        )
        self.cursor.execute.assert_not_called()

    def test_first_upload_accepts_zero_without_a_previous_row(self):
        self.cursor.fetchone.return_value = None
        merged = self.bridge._merge_latest_result_payload(
            self.conn, "new-station", {"fastAvailable": 0, "fastTotal": 22},
        )
        self.assertEqual(merged["fastAvailable"], "0")
        self.assertEqual(merged["fastTotal"], "22")
        self.assertEqual(merged["superTotal"], "")


if __name__ == "__main__":
    unittest.main()
