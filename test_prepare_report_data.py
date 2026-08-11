import unittest

from prepare_report_data import equipment_summary, parse_region, price_schedule_text


class PrepareReportDataTests(unittest.TestCase):
    def test_parse_region_from_full_henan_address(self):
        self.assertEqual(
            ("洛阳市", "新安县"),
            parse_region("河南省洛阳市新安县铁门镇盐仓村"),
        )

    def test_parse_region_ignores_non_administrative_market_name(self):
        self.assertEqual(("", ""), parse_region("白霜超市路口"))

    def test_parse_region_maps_county_city_to_prefecture(self):
        self.assertEqual(
            ("郑州市", "新密市"),
            parse_region("新密市岳村镇地面停车场"),
        )

    def test_parse_region_uses_station_name_when_address_is_short(self):
        self.assertEqual(
            ("郑州市", "登封市"),
            parse_region("园区路", "特来电郑州登封东华镇工业园重卡充电站"),
        )

    def test_equipment_and_price_summaries_are_readable(self):
        result = {
            "fast_available": "3",
            "fast_total": "4",
            "fast_power": "240kW",
            "fast_prices": [
                {
                    "time": "00:00-07:00",
                    "total_price": "0.69",
                    "elec_fee": "0.37",
                    "service_fee": "0.32",
                }
            ],
        }

        self.assertEqual("快充 3/4 240kW", equipment_summary(result))
        self.assertIn("00:00-07:00 0.69", price_schedule_text(result))


if __name__ == "__main__":
    unittest.main()
