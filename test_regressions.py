import asyncio
import threading
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, mock_open, patch

from starlette.routing import Match

import api_server
from batch_runner import evaluate_detail
from crawler import AmapCrawler, parse_detail_xml
from page_state import PageKind


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
