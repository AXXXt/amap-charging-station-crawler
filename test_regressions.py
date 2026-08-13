import asyncio
import threading
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, mock_open, patch

from starlette.routing import Match

import api_server
from batch_runner import evaluate_detail
from crawler import (
    AmapCrawler,
    DISTRICT_MATCH_OUTSIDE,
    DISTRICT_MATCH_TARGET,
    DISTRICT_MATCH_UNKNOWN,
    SCROLL_TOWARD_TOP,
    classify_district_match,
    classify_detail_page,
    find_price_detail_entry,
    merge_price_periods,
    parse_detail_xml,
    parse_price_detail_page,
)
from page_state import PageAssessment, PageKind


class CrawlerRegressionTests(unittest.TestCase):
    def test_basic_detail_address_and_missing_hours_are_normalized(self):
        xml = """<hierarchy>
            <node content-desc="汽车充电站(测试站)" />
            <node text="地上 | 人民路160号" />
            <node text="暂无营业时间" />
        </hierarchy>"""

        result = parse_detail_xml(xml)

        self.assertEqual("人民路160号", result["address"])
        self.assertEqual("暂无营业时间", result["business_hours"])

    def test_compact_detail_extracts_business_status_as_hours(self):
        xml = """<hierarchy>
            <node content-desc="陇海路临湖路重卡汽车充电站" />
            <node text="营业中" />
            <node text="24小时营业" />
        </hierarchy>"""

        result = parse_detail_xml(xml)

        self.assertEqual("24小时营业", result["business_hours"])

    def test_supercharge_station_name_is_extracted(self):
        xml = """<hierarchy>
            <node content-desc="铁门锦阳重卡超充站(星轲能源JM)" />
            <node text="铁门镇" />
        </hierarchy>"""

        result = parse_detail_xml(xml)

        self.assertEqual("铁门锦阳重卡超充站(星轲能源JM)", result["station_name"])

    def test_heavy_truck_and_swap_station_names_are_extracted(self):
        for station_name in (
            "淇县窦氏重卡站",
            "汽车充换电站(启源充换电站安阳安林路站)",
        ):
            with self.subTest(station_name=station_name):
                result = parse_detail_xml(
                    f'<hierarchy><node content-desc="{station_name}" /></hierarchy>'
                )
                self.assertEqual(station_name, result["station_name"])

    def test_poi_summary_card_infers_operator_and_embedded_facility(self):
        xml = """<hierarchy>
            <node content-desc="云快充汽车充电站(畅行重卡2站)" />
            <node text="刚刚浏览" />
            <node text="距云快充汽车充电站(畅行重卡2站)·地上｜昆仑能源西北侧" />
            <node text="暂无更多内容" />
        </hierarchy>"""

        result = parse_detail_xml(xml)

        self.assertEqual("云快充汽车充电站(畅行重卡2站)", result["station_name"])
        self.assertEqual("云快充", result["operator"])
        self.assertIn("地上", result["facilities"])

    def test_wait_for_page_honors_stop_event_immediately(self):
        class FailIfUsedDevice:
            def dump_hierarchy(self):
                raise AssertionError("device should not be queried after stop")

        crawler = AmapCrawler.__new__(AmapCrawler)
        crawler.d = FailIfUsedDevice()
        crawler.stop_event = threading.Event()
        crawler.stop_event.set()

        confirmed, assessment, xml_text = crawler._wait_for_page(PageKind.DETAIL)

        self.assertFalse(confirmed)
        self.assertIsNone(assessment)
        self.assertEqual("", xml_text)

    def test_poi_detail_wait_uses_expected_station_name(self):
        crawler = AmapCrawler.__new__(AmapCrawler)
        crawler.serial = "device"
        crawler._wait_for_page = MagicMock(return_value=(True, None, ""))
        station = {"id": "B0TEST", "name": "汽车充电站(测试站)"}

        with patch("crawler.subprocess.run") as run:
            run.return_value.returncode = 0
            run.return_value.stdout = ""
            run.return_value.stderr = ""
            opened = crawler._open_poi_detail(station)

        self.assertTrue(opened)
        crawler._wait_for_page.assert_called_once_with(
            PageKind.DETAIL,
            expected_station=station["name"],
            timeout=8,
        )

    def test_incremental_scan_handles_visible_station_before_first_swipe(self):
        xml = """<hierarchy>
            <node clickable="true"
                  content-desc="测试重卡充电站"
                  bounds="[36,700][1044,1200]" />
        </hierarchy>"""

        class FakeDevice:
            def __init__(self):
                self.swipe_count = 0

            def dump_hierarchy(self):
                return xml

            def window_size(self):
                return 1080, 2400

            def swipe(self, *args, **kwargs):
                self.swipe_count += 1

        crawler = AmapCrawler.__new__(AmapCrawler)
        crawler.d = FakeDevice()
        crawler.stop_event = threading.Event()
        handled = []

        def handle_station(station):
            handled.append(station)
            crawler.stop_event.set()

        discovered = crawler._scan_search_results_incrementally(
            "郑州中原区重卡充电站",
            handle_station,
        )

        self.assertEqual(1, len(discovered))
        self.assertEqual("测试重卡充电站", handled[0]["name"])
        self.assertTrue(handled[0]["_click_visible"])
        self.assertEqual(0, crawler.d.swipe_count)

    def test_search_card_extraction_keeps_visible_address(self):
        xml = """<hierarchy>
            <node clickable="true"
                  content-desc="达克云超级充电站(鼎盛鸿丰裕物流园站)"
                  bounds="[36,700][1044,1200]">
                <node text="河南省郑州市新郑市德坤物流E园西南侧310米" />
                <node text="66公里" />
            </node>
        </hierarchy>"""

        cards = AmapCrawler._extract_visible_station_cards(xml)

        self.assertEqual(1, len(cards))
        self.assertEqual(
            "河南省郑州市新郑市德坤物流E园西南侧310米",
            cards[0]["address"],
        )

    def test_search_card_extraction_ignores_highlighted_title(self):
        xml = """<hierarchy>
            <node clickable="true"
                  content-desc="小桔充电汽车充电站(郑州中原区电厂路充电站)"
                  bounds="[36,700][1044,1200]">
                <node text="&lt;font&gt;郑州中原区电厂路充电站&lt;/font&gt;" />
                <node text="电厂路与电厂南路交叉口东北角地面停车场" />
            </node>
        </hierarchy>"""

        cards = AmapCrawler._extract_visible_station_cards(xml)

        self.assertEqual("", cards[0]["address"])

    def test_district_match_recognizes_unlisted_city_functional_area(self):
        match = classify_district_match(
            "郑州",
            "中原区",
            "河南省郑州市航空港区炎黄大道与文化路交叉口",
        )

        self.assertEqual(DISTRICT_MATCH_OUTSIDE, match)

    def test_district_match_does_not_treat_residential_area_as_district(self):
        match = classify_district_match(
            "郑州",
            "中原区",
            "河南省郑州市建设路幸福小区东门",
        )

        self.assertEqual(DISTRICT_MATCH_UNKNOWN, match)

    def test_district_match_uses_station_name_only_as_target_hint(self):
        outside_name = classify_district_match(
            "郑州",
            "中原区",
            "",
            target_hints=("新郑市物流园重卡充电站",),
        )
        target_name = classify_district_match(
            "郑州",
            "中原区",
            "",
            target_hints=("郑州中原区电厂路充电站",),
        )

        self.assertEqual(DISTRICT_MATCH_UNKNOWN, outside_name)
        self.assertEqual(DISTRICT_MATCH_TARGET, target_name)

    def test_incremental_scan_stops_when_handler_requests_it(self):
        xml = """<hierarchy>
            <node clickable="true"
                  content-desc="郑州公用集团中原超级充电站"
                  bounds="[36,700][1044,1200]" />
        </hierarchy>"""

        class FakeDevice:
            def __init__(self):
                self.swipe_count = 0

            def dump_hierarchy(self):
                return xml

            def window_size(self):
                return 1080, 2400

            def swipe(self, *args, **kwargs):
                self.swipe_count += 1

        crawler = AmapCrawler.__new__(AmapCrawler)
        crawler.d = FakeDevice()
        crawler.stop_event = None

        discovered = crawler._scan_search_results_incrementally(
            "郑州中原区重卡充电站",
            lambda station: False,
        )

        self.assertEqual(1, len(discovered))
        self.assertEqual(0, crawler.d.swipe_count)

    def test_visible_station_uses_current_card_coordinates(self):
        crawler = AmapCrawler.__new__(AmapCrawler)
        crawler.d = MagicMock()
        crawler.stop_event = None
        crawler._confirm_detail_page = MagicMock(return_value=True)

        opened = crawler._open_visible_station(
            {"name": "测试重卡充电站", "cx": 540, "cy": 960}
        )

        self.assertTrue(opened)
        crawler.d.click.assert_called_once_with(540, 960)

    def test_search_query_uses_focused_field_for_unicode_text(self):
        crawler = AmapCrawler.__new__(AmapCrawler)
        crawler.d = MagicMock()
        focused_input = MagicMock()
        focused_input.exists = True
        crawler.d.return_value = focused_input

        crawler._enter_search_query("郑州中原区重卡充电站")

        focused_input.set_text.assert_called_once_with("郑州中原区重卡充电站")
        crawler.d.send_keys.assert_not_called()

    def test_price_trend_entry_uses_clickable_trend_block(self):
        xml = """<hierarchy>
            <node clickable="true" bounds="[835,1476][1008,1578]">
                <node text="查看详情" bounds="[835,1503][972,1551]" />
            </node>
            <node clickable="true" bounds="[48,1614][1032,2025]">
                <node clickable="true" bounds="[48,1626][1032,1674]">
                    <node text="24小时价格趋势图" bounds="[108,1626][387,1674]" />
                </node>
                <node text="￥0.64/度" bounds="[105,1710][234,1752]" />
            </node>
        </hierarchy>"""

        entry = find_price_detail_entry(xml)
        page_info = classify_detail_page(xml)

        self.assertEqual("price_trend", entry["marker"])
        self.assertEqual((48, 1614, 1032, 2025), entry["bounds"])
        self.assertEqual((540, 1819), (entry["cx"], entry["cy"]))
        self.assertEqual("full_trend", page_info["type"])
        self.assertTrue(page_info["features"]["has_price_detail_entry"])

    def test_price_card_entry_requires_price_context(self):
        price_card_xml = """<hierarchy>
            <node clickable="true" bounds="[48,1200][1032,1500]">
                <node text="0.64" />
                <node text="/度" />
                <node text="查看" />
            </node>
        </hierarchy>"""
        unrelated_xml = """<hierarchy>
            <node clickable="true" bounds="[48,900][1032,1100]">
                <node text="0.64" />
                <node text="/度" />
            </node>
            <node clickable="true" bounds="[835,1476][1008,1578]">
                <node text="查看详情" />
            </node>
        </hierarchy>"""

        entry = find_price_detail_entry(price_card_xml)

        self.assertEqual("price_card", entry["marker"])
        self.assertEqual((540, 1350), (entry["cx"], entry["cy"]))
        self.assertIsNone(find_price_detail_entry(unrelated_xml))

    def test_price_card_entry_rejects_large_shared_clickable_container(self):
        xml = """<hierarchy>
            <node clickable="true" bounds="[0,100][1080,2200]">
                <node bounds="[48,300][1032,520]">
                    <node text="0.64" />
                    <node text="/度" />
                </node>
                <node bounds="[48,1600][1032,1900]">
                    <node text="停车费" />
                    <node text="查看" />
                    <node text="查看详情" />
                </node>
            </node>
        </hierarchy>"""

        self.assertIsNone(find_price_detail_entry(xml))

    def test_price_detail_parser_supports_inline_and_split_fees(self):
        xml = """<hierarchy>
            <node text="充电价格详情" />
            <node text="00:00-07:00 参考价0.64/度 电费：￥0.37/度 服务费：￥0.27/度" />
            <node text="07:00-16:00" />
            <node text="参考价" />
            <node text="0.88" />
            <node text="电费" />
            <node text="￥0.58/度" />
            <node text="服务费" />
            <node text="￥0.30/度" />
        </hierarchy>"""

        periods = parse_price_detail_page(xml)

        self.assertEqual(2, len(periods))
        self.assertEqual(
            {
                "time": "00:00-07:00",
                "total_price": "0.64",
                "elec_fee": "0.37",
                "service_fee": "0.27",
                "tag": "",
                "source": "price_detail",
            },
            periods[0],
        )
        self.assertEqual("0.88", periods[1]["total_price"])
        self.assertEqual("0.58", periods[1]["elec_fee"])
        self.assertEqual("0.30", periods[1]["service_fee"])

    def test_price_detail_parser_merges_non_adjacent_duplicate_periods(self):
        xml = """<hierarchy>
            <node text="充电价格详情" />
            <node text="00:00-07:00 参考价0.64/度 电费：￥0.37/度" />
            <node text="07:00-16:00 参考价0.88/度 电费：￥0.58/度 服务费：￥0.30/度" />
            <node text="00:00-07:00 服务费：￥0.27/度" />
        </hierarchy>"""

        periods = parse_price_detail_page(xml)

        self.assertEqual(2, len(periods))
        self.assertEqual("00:00-07:00", periods[0]["time"])
        self.assertEqual("0.64", periods[0]["total_price"])
        self.assertEqual("0.37", periods[0]["elec_fee"])
        self.assertEqual("0.27", periods[0]["service_fee"])

    def test_embedded_trend_uses_unified_price_fields(self):
        xml = """<hierarchy>
            <node text="24小时价格趋势图" />
            <node text="￥0.64/度" />
            <node text="00:00-07:00" />
        </hierarchy>"""

        period = parse_detail_xml(xml)["fast_prices"][0]

        self.assertNotIn("price", period)
        self.assertEqual("0.64", period["total_price"])
        self.assertEqual("", period["elec_fee"])
        self.assertEqual("", period["service_fee"])
        self.assertEqual("embedded_trend", period["source"])

    def test_price_period_merge_prefers_detail_and_adds_missing_trend_period(self):
        detail_periods = [{
            "time": "00:00-07:00",
            "total_price": "0.64",
            "elec_fee": "0.37",
            "service_fee": "0.27",
        }]
        fallback_periods = [
            {"time": "00:00-07:00", "price": "0.63"},
            {"time": "07:00-16:00", "price": "0.88"},
            {"time": "当前时段", "price": "0.64"},
        ]

        periods = merge_price_periods(detail_periods, fallback_periods)

        self.assertEqual(2, len(periods))
        self.assertEqual("0.64", periods[0]["total_price"])
        self.assertEqual("0.37", periods[0]["elec_fee"])
        self.assertEqual("price_detail", periods[0]["source"])
        self.assertEqual("07:00-16:00", periods[1]["time"])
        self.assertEqual("0.88", periods[1]["total_price"])
        self.assertEqual("embedded_trend", periods[1]["source"])

    def test_collect_price_detail_clicks_entry_and_returns_to_station(self):
        detail_xml = """<hierarchy>
            <node clickable="true" bounds="[48,1614][1032,2025]">
                <node text="24小时价格趋势图" />
            </node>
        </hierarchy>"""
        price_xml = """<hierarchy>
            <node text="充电价格详情" />
            <node text="00:00-07:00 参考价0.64/度 电费：￥0.37/度 服务费：￥0.27/度" />
        </hierarchy>"""
        crawler = AmapCrawler.__new__(AmapCrawler)
        crawler.d = MagicMock()
        crawler.stop_event = None
        crawler._wait_for_page = MagicMock(side_effect=[
            (True, PageAssessment(PageKind.PRICE_DETAIL, 0.98, ()), price_xml),
            (True, PageAssessment(PageKind.DETAIL, 0.98, (), True), detail_xml),
        ])

        periods, _, returned = crawler._collect_price_detail(detail_xml, "测试重卡充电站")

        self.assertTrue(returned)
        self.assertEqual("0.37", periods[0]["elec_fee"])
        crawler.d.click.assert_called_once_with(540, 1819)
        crawler.d.press.assert_called_once_with("back")

    def test_collect_price_detail_keeps_fallback_when_click_does_not_navigate(self):
        detail_xml = """<hierarchy>
            <node content-desc="测试重卡充电站" />
            <node clickable="true" bounds="[48,1614][1032,2025]">
                <node text="24小时价格趋势图" />
            </node>
        </hierarchy>"""
        crawler = AmapCrawler.__new__(AmapCrawler)
        crawler.d = MagicMock()
        crawler.stop_event = None
        crawler._wait_for_page = MagicMock(return_value=(
            False,
            PageAssessment(PageKind.DETAIL, 0.95, (), True),
            detail_xml,
        ))

        periods, _, returned = crawler._collect_price_detail(detail_xml, "测试重卡充电站")

        self.assertTrue(returned)
        self.assertEqual([], periods)
        crawler.d.press.assert_not_called()

    def test_price_detail_return_scrolls_up_to_verify_original_station(self):
        station_name = "云快充汽车充电站(易岸达物流园充电站)"
        detail_xml = """<hierarchy>
            <node clickable="true" bounds="[48,1400][1032,2050]">
                <node text="24小时价格趋势图" />
            </node>
        </hierarchy>"""
        price_xml = """<hierarchy>
            <node text="充电价格详情" />
            <node text="00:00-07:00 参考价0.89/度 电费：￥0.62/度 服务费：￥0.27/度" />
        </hierarchy>"""
        low_detail_xml = """<hierarchy>
            <node text="电站信息" />
            <node text="24小时价格趋势图" />
        </hierarchy>"""
        top_detail_xml = f"""<hierarchy>
            <node content-desc="{station_name}" />
            <node text="电站信息" />
        </hierarchy>"""
        crawler = AmapCrawler.__new__(AmapCrawler)
        crawler.d = MagicMock()
        crawler.d.dump_hierarchy.return_value = top_detail_xml
        crawler.stop_event = None
        crawler._wait_for_page = MagicMock(side_effect=[
            (True, PageAssessment(PageKind.PRICE_DETAIL, 0.98, ()), price_xml),
            (False, PageAssessment(PageKind.UNKNOWN, 0.2, (), False), low_detail_xml),
        ])

        with patch("crawler.time.sleep"):
            periods, _, returned = crawler._collect_price_detail(detail_xml, station_name)

        self.assertTrue(returned)
        self.assertEqual("0.62", periods[0]["elec_fee"])
        crawler.d.swipe.assert_called_once_with(*SCROLL_TOWARD_TOP, duration=0.35)

    def test_price_entry_must_clear_bottom_overlay_before_clicking(self):
        crawler = AmapCrawler.__new__(AmapCrawler)
        crawler.d = MagicMock()
        crawler.d.window_size.return_value = (1080, 2340)

        hidden_entry = {
            "bounds": (48, 1827, 1032, 2340),
            "cx": 540,
            "cy": 2083,
        }
        visible_entry = {
            "bounds": (48, 1447, 1032, 1858),
            "cx": 540,
            "cy": 1652,
        }

        self.assertFalse(crawler._price_entry_is_safely_clickable(hidden_entry))
        self.assertTrue(crawler._price_entry_is_safely_clickable(visible_entry))

    def test_collect_detail_prefers_price_breakdown_over_embedded_trend(self):
        crawler = AmapCrawler.__new__(AmapCrawler)
        crawler.d = MagicMock()
        crawler.d.dump_hierarchy.return_value = """<hierarchy>
            <node content-desc="测试重卡充电站" />
            <node text="电站信息" />
            <node clickable="true" bounds="[48,1400][1032,2050]">
                <node text="24小时价格趋势图" />
            </node>
        </hierarchy>"""
        crawler.stop_event = None
        crawler._open_visible_station = MagicMock(return_value=True)
        crawler._collect_price_detail = MagicMock(return_value=([
            {
                "time": "00:00-07:00",
                "total_price": "0.64",
                "elec_fee": "0.37",
                "service_fee": "0.27",
            }
        ], {}, True))
        initial_result = {
            "station_name": "测试重卡充电站",
            "address": "郑州市中原区测试路1号",
            "fast_prices": [{"time": "00:00-07:00", "price": "0.63"}],
            "slow_prices": [],
        }
        initial_result.update({
            key: "" for key in (
                "operator", "business_hours", "current_price", "parking_fee",
                "occupancy_fee", "longitude", "latitude",
            )
        })

        with (
            patch("crawler.assess_page", return_value=PageAssessment(
                PageKind.DETAIL, 0.98, (), True,
            )),
            patch("crawler.classify_detail_page", return_value={
                "type": "full_trend",
                "description": "完整：含24h价格趋势图",
                "scrolls_needed": 0,
                "features": {"has_price_detail_entry": True},
            }),
            patch("crawler.parse_detail_xml", return_value=initial_result),
        ):
            result = crawler.collect_detail(
                {
                    "name": "测试重卡充电站",
                    "_click_visible": True,
                    "longitude": 113.1,
                    "latitude": 34.7,
                },
                "郑州",
            )

        self.assertEqual("price_detail", result["price_schedule_source"])
        self.assertTrue(result["price_detail_attempted"])
        self.assertTrue(result["price_detail_collected"])
        self.assertEqual("0.64", result["fast_prices"][0]["total_price"])
        self.assertEqual("0.37", result["fast_prices"][0]["elec_fee"])
        self.assertEqual("0.27", result["fast_prices"][0]["service_fee"])

    def test_collect_detail_detects_price_entry_after_second_scroll(self):
        self._assert_delayed_price_entry_is_collected(entry_scroll=2)

    def test_collect_detail_detects_price_entry_after_third_scroll(self):
        self._assert_delayed_price_entry_is_collected(entry_scroll=3)

    def test_collect_detail_waits_for_trend_card_to_clear_bottom_overlay(self):
        station_name = "云快充汽车充电站(易岸达物流园充电站)"
        hidden_trend_xml = f"""<hierarchy>
            <node content-desc="{station_name}" />
            <node text="电站信息" />
            <node clickable="true" bounds="[48,1827][1032,2340]">
                <node text="24小时价格趋势图" />
                <node text="￥0.89/度" />
            </node>
        </hierarchy>"""
        still_hidden_xml = f"""<hierarchy>
            <node content-desc="{station_name}" />
            <node text="电站信息" />
            <node clickable="true" bounds="[48,1855][1032,2266]">
                <node text="24小时价格趋势图" />
                <node text="￥0.89/度" />
            </node>
        </hierarchy>"""
        visible_trend_xml = f"""<hierarchy>
            <node content-desc="{station_name}" />
            <node text="电站信息" />
            <node clickable="true" bounds="[48,1447][1032,1858]">
                <node text="24小时价格趋势图" />
                <node text="￥0.89/度" />
                <node text="00:00-07:00" />
            </node>
        </hierarchy>"""

        crawler = AmapCrawler.__new__(AmapCrawler)
        crawler.d = MagicMock()
        crawler.d.window_size.return_value = (1080, 2340)
        crawler.d.dump_hierarchy.side_effect = [
            hidden_trend_xml,
            still_hidden_xml,
            visible_trend_xml,
        ]
        crawler.stop_event = None
        crawler._open_visible_station = MagicMock(return_value=True)
        crawler._collect_price_detail = MagicMock(return_value=([{
            "time": "00:00-07:00",
            "total_price": "0.89",
            "elec_fee": "0.62",
            "service_fee": "0.27",
        }], {}, True))

        with patch("crawler.time.sleep"):
            result = crawler.collect_detail(
                {
                    "name": station_name,
                    "_click_visible": True,
                    "longitude": 113.1,
                    "latitude": 34.7,
                },
                "郑州",
            )

        self.assertEqual(2, crawler.d.swipe.call_count)
        crawler._collect_price_detail.assert_called_once_with(
            visible_trend_xml,
            station_name,
        )
        self.assertTrue(result["price_detail_collected"])

    def test_collect_detail_stops_price_probe_at_bottom(self):
        station_name = "测试基础充电站"
        initial_xml = f"""<hierarchy>
            <node content-desc="{station_name}" />
            <node text="电站信息" />
            <node text="快充" />
            <node text="空闲2/4" />
            <node text="￥0.89/度" />
        </hierarchy>"""
        bottom_xml = f"""<hierarchy>
            <node content-desc="{station_name}" />
            <node text="电站信息" />
            <node text="暂无更多内容" />
        </hierarchy>"""
        crawler = AmapCrawler.__new__(AmapCrawler)
        crawler.d = MagicMock()
        crawler.d.dump_hierarchy.side_effect = [initial_xml, bottom_xml]
        crawler.stop_event = None
        crawler._open_visible_station = MagicMock(return_value=True)
        crawler._collect_price_detail = MagicMock()

        with patch("crawler.time.sleep"):
            result = crawler.collect_detail(
                {
                    "name": station_name,
                    "_click_visible": True,
                    "longitude": 113.1,
                    "latitude": 34.7,
                },
                "郑州",
            )

        self.assertEqual(1, crawler.d.swipe.call_count)
        crawler._collect_price_detail.assert_not_called()
        self.assertFalse(result["price_detail_attempted"])
        self.assertFalse(result["price_detail_collected"])

    def test_collect_detail_skips_delayed_probe_without_price_signals(self):
        station_name = "测试基础充电站"
        initial_xml = f"""<hierarchy>
            <node content-desc="{station_name}" />
            <node text="营业时间" />
            <node text="24小时营业" />
            <node text="地图" />
            <node text="电话" />
        </hierarchy>"""
        crawler = AmapCrawler.__new__(AmapCrawler)
        crawler.d = MagicMock()
        crawler.d.dump_hierarchy.return_value = initial_xml
        crawler.stop_event = None
        crawler._open_visible_station = MagicMock(return_value=True)
        crawler._collect_price_detail = MagicMock()

        with patch("crawler.time.sleep"):
            result = crawler.collect_detail(
                {
                    "name": station_name,
                    "_click_visible": True,
                    "longitude": 113.1,
                    "latitude": 34.7,
                },
                "郑州",
            )

        crawler.d.swipe.assert_not_called()
        crawler._collect_price_detail.assert_not_called()
        self.assertFalse(result["price_detail_attempted"])

    def _assert_delayed_price_entry_is_collected(self, entry_scroll):
        station_name = "云快充汽车充电站(易岸达物流园充电站)"
        initial_xml = f"""<hierarchy>
            <node content-desc="{station_name}" />
            <node text="电站信息" />
            <node text="快充" />
            <node text="空闲11/20" />
            <node text="￥0.89/度" />
        </hierarchy>"""
        trend_xml = f"""<hierarchy>
            <node content-desc="{station_name}" />
            <node text="电站信息" />
            <node clickable="true" bounds="[48,1400][1032,2050]">
                <node text="24小时价格趋势图" />
                <node text="￥0.89/度" />
                <node text="00:00-07:00" />
            </node>
        </hierarchy>"""
        plain_scroll_xml = f"""<hierarchy>
            <node content-desc="{station_name}" />
            <node text="电站信息" />
            <node text="高德专享价" />
        </hierarchy>"""
        scroll_xmls = [plain_scroll_xml] * (entry_scroll - 1) + [trend_xml]

        crawler = AmapCrawler.__new__(AmapCrawler)
        crawler.d = MagicMock()
        crawler.d.dump_hierarchy.side_effect = [initial_xml, *scroll_xmls]
        crawler.stop_event = None
        crawler._open_visible_station = MagicMock(return_value=True)
        crawler._collect_price_detail = MagicMock(return_value=([{
            "time": "00:00-07:00",
            "total_price": "0.89",
            "elec_fee": "0.62",
            "service_fee": "0.27",
        }], {}, True))

        with patch("crawler.time.sleep"):
            result = crawler.collect_detail(
                {
                    "name": station_name,
                    "_click_visible": True,
                    "longitude": 113.1,
                    "latitude": 34.7,
                },
                "郑州",
            )

        self.assertEqual(entry_scroll, crawler.d.swipe.call_count)
        crawler._collect_price_detail.assert_called_once_with(trend_xml, station_name)
        self.assertTrue(result["price_detail_attempted"])
        self.assertTrue(result["price_detail_collected"])
        self.assertEqual("price_detail", result["price_schedule_source"])
        self.assertEqual("0.62", result["fast_prices"][0]["elec_fee"])

    def test_merge_results_combines_price_periods_from_multiple_scrolls(self):
        first = parse_detail_xml("""<hierarchy>
            <node text="24小时价格趋势图" />
            <node text="￥0.64/度" />
            <node text="00:00-07:00" />
        </hierarchy>""")
        second = parse_detail_xml("""<hierarchy>
            <node text="24小时价格趋势图" />
            <node text="￥0.88/度" />
            <node text="07:00-16:00" />
        </hierarchy>""")

        from crawler import merge_results

        periods = merge_results(first, second)["fast_prices"]

        self.assertEqual(["00:00-07:00", "07:00-16:00"], [
            period["time"] for period in periods
        ])

    def test_verified_heavy_truck_station_counts_as_detailed(self):
        assessment = evaluate_detail({
            "detail_verified": True,
            "station_name": "淇县窦氏重卡站",
            "address": "河南省鹤壁市淇县窦氏运输有限公司",
            "business_hours": "暂无营业时间",
            "current_price": "0.57",
        })

        self.assertTrue(assessment["detailed"])

    def test_district_filter_scans_past_consecutive_mismatches(self):
        crawler = AmapCrawler.__new__(AmapCrawler)
        crawler.results = []
        crawler.stop_event = None
        stations = [{"name": "one"}, {"name": "two"}, {"name": "three"}]
        details = iter([
            {"station_name": "one", "address": "洛阳市涧西区"},
            {"station_name": "two", "address": "洛阳市老城区"},
            {"station_name": "three", "address": "洛阳市新安县"},
        ])

        def search_stations(
            city,
            query=None,
            recenter_only=False,
            station_handler=None,
        ):
            if recenter_only:
                return []
            for station in stations:
                station_handler(station)
            return stations

        crawler.search_stations = search_stations
        crawler.collect_detail = lambda station, city: next(details)

        with patch("time.sleep", return_value=None):
            count = crawler.run_district("洛阳", "新安县")

        self.assertEqual(1, count)
        self.assertEqual(["three"], [item["station_name"] for item in crawler.results])

    def test_district_filter_uses_search_card_address_when_detail_is_abbreviated(self):
        crawler = AmapCrawler.__new__(AmapCrawler)
        crawler.results = []
        crawler.stop_event = None
        station = {
            "name": "云快充汽车充电站(易岸达物流园充电站)",
            "address": "河南省郑州市中原区梧桐西街2号",
        }

        def search_stations(city, query=None, station_handler=None, **kwargs):
            station_handler(station)
            return [station]

        crawler.search_stations = search_stations
        crawler.collect_detail = MagicMock(return_value={
            "station_name": station["name"],
            "address": "梧桐西街2号",
        })

        count, mismatch_count = crawler._collect_district_search(
            "郑州",
            "中原区",
            "郑州中原区重卡充电站",
        )

        self.assertEqual(1, count)
        self.assertEqual(0, mismatch_count)
        self.assertEqual([station["name"]], [item["station_name"] for item in crawler.results])

    def test_district_filter_stops_after_confirmed_outside_tail(self):
        crawler = AmapCrawler.__new__(AmapCrawler)
        crawler.results = []
        crawler.stop_event = None
        stations = [
            {"name": "target-one", "address": "河南省郑州市中原区建设路1号"},
            {"name": "target-two", "address": "河南省郑州市中原区桐柏路2号"},
            {"name": "early-outlier", "address": "河南省郑州市新郑市薛店镇3号"},
            {"name": "target-three", "address": "河南省郑州市中原区电厂路4号"},
            {"name": "outside-one", "address": "河南省郑州市惠济区大河路5号"},
            {"name": "outside-two", "address": "河南省郑州市金水区货站街6号"},
            {"name": "outside-three", "address": "河南省郑州市荥阳市中原路7号"},
            {"name": "outside-four", "address": "河南省郑州市管城回族区航海路8号"},
            {"name": "late-target", "address": "河南省郑州市中原区西四环9号"},
        ]
        visited = []

        def search_stations(city, query=None, station_handler=None, **kwargs):
            discovered = []
            for station in stations:
                discovered.append(station)
                if station_handler(station) is False:
                    break
            return discovered

        def collect_detail(station, city):
            visited.append(station["name"])
            return {
                "station_name": station["name"],
                "address": station["address"],
            }

        crawler.search_stations = search_stations
        crawler.collect_detail = collect_detail

        count, mismatch_count = crawler._collect_district_search(
            "郑州",
            "中原区",
            "郑州中原区重卡充电站",
        )

        self.assertEqual(3, count)
        self.assertEqual(5, mismatch_count)
        self.assertEqual(["target-one", "target-two", "target-three"], visited)
        self.assertNotIn("late-target", visited)

    def test_district_filter_does_not_stop_on_unknown_addresses(self):
        crawler = AmapCrawler.__new__(AmapCrawler)
        crawler.results = []
        crawler.stop_event = None
        stations = [
            {"name": "target-one", "address": "河南省郑州市中原区建设路1号"},
            {"name": "unknown-one"},
            {"name": "unknown-two"},
            {"name": "unknown-three"},
            {"name": "unknown-four"},
            {"name": "target-two", "address": "河南省郑州市中原区西四环2号"},
        ]

        def search_stations(city, query=None, station_handler=None, **kwargs):
            for station in stations:
                if station_handler(station) is False:
                    break
            return stations

        def collect_detail(station, city):
            return {
                "station_name": station["name"],
                "address": station.get("address", "未知道路"),
            }

        crawler.search_stations = search_stations
        crawler.collect_detail = collect_detail

        count, mismatch_count = crawler._collect_district_search(
            "郑州",
            "中原区",
            "郑州中原区重卡充电站",
        )

        self.assertEqual(2, count)
        self.assertEqual(4, mismatch_count)
        self.assertEqual(
            ["target-one", "target-two"],
            [item["station_name"] for item in crawler.results],
        )


class ApiRegressionTests(unittest.TestCase):
    def setUp(self):
        api_server.stop_event.clear()
        api_server.task_state["running"] = False
        api_server.task_state["progress"] = {"done": 0, "total": 0}

    def tearDown(self):
        api_server.stop_event.clear()
        api_server.task_state["running"] = False

    def test_nearby_route_is_not_captured_by_station_id_route(self):
        detail_route = next(
            route
            for route in api_server.app.routes
            if getattr(route, "path", "") == "/api/stations/{station_id:int}"
        )
        nearby_route = next(
            route
            for route in api_server.app.routes
            if getattr(route, "path", "") == "/api/stations/nearby"
        )
        scope = {
            "type": "http",
            "path": "/api/stations/nearby",
            "root_path": "",
            "method": "GET",
        }

        self.assertEqual(Match.NONE, detail_route.matches(scope)[0])
        self.assertEqual(Match.FULL, nearby_route.matches(scope)[0])

    def test_progress_wrappers_are_installed_once(self):
        class FakeCrawler:
            instance = None

            def __init__(self, visual_checker=None, stop_event=None):
                type(self).instance = self
                self.results = []
                self.search_calls = 0
                self.collect_calls = 0

            def search_stations(
                self,
                city,
                query=None,
                recenter_only=False,
                station_handler=None,
            ):
                self.search_calls += 1
                if station_handler is not None:
                    station_handler({"name": city})
                return []

            def collect_detail(self, station, city):
                self.collect_calls += 1
                return None

            def run_city(self, city):
                self.search_stations(city, f"{city}-one")
                self.search_stations(
                    city,
                    f"{city}-two",
                    station_handler=lambda station: self.collect_detail(station, city),
                )
                return 0

            def deduplicate_results(self):
                return None

        api_server.task_state["running"] = True
        with (
            patch("crawler.AmapCrawler", FakeCrawler),
            patch("builtins.open", mock_open()),
            patch("json.dump"),
        ):
            api_server._run_crawl_task(["city-a", "city-b"])

        instance = FakeCrawler.instance
        self.assertEqual(4, instance.search_calls)
        self.assertEqual(2, instance.collect_calls)
        self.assertEqual(2, api_server.task_state["progress"]["done"])

    def test_start_marks_task_running_before_thread_start(self):
        observed = {}

        class FakeThread:
            def __init__(self, *args, **kwargs):
                pass

            def start(self):
                observed["running"] = api_server.task_state["running"]

        with patch("api_server.threading.Thread", FakeThread):
            result = asyncio.run(api_server.start_task("洛阳", False, None))

        self.assertTrue(observed["running"])
        self.assertEqual("started", result["status"])

    def test_user_station_api_enqueues_high_priority_task(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "queue.db"
            request = api_server.UserStationTask(
                id="USER-POI",
                name="用户返回充电站",
                address="测试路1号",
                latitude=34.1,
                longitude=113.1,
            )
            with patch("api_server.QUEUE_DB_PATH", str(db_path)):
                response = asyncio.run(api_server.enqueue_station_task(request))
                status = asyncio.run(api_server.station_queue_status())

        self.assertEqual("queued", response["status"])
        self.assertEqual(1000, response["priority"])
        self.assertEqual(1, status["pending"])


if __name__ == "__main__":
    unittest.main()
