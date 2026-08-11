import unittest
from pathlib import Path

from page_state import PageKind, assess_page, normalize_station_name


class PageStateTests(unittest.TestCase):
    def test_search_cards_are_not_detail_page(self):
        xml = """<hierarchy>
            <node text="在此区域搜索" />
            <node text="停车费 免费" />
            <node text="空闲" />
            <node text="￥0.51/度" />
            <node content-desc="测试重卡充电站" clickable="true" />
        </hierarchy>"""
        assessment = assess_page(xml, "测试重卡充电站")
        self.assertEqual(PageKind.SEARCH_RESULTS, assessment.kind)

    def test_scrolled_search_results_without_area_action(self):
        xml = """<hierarchy>
            <node text="扫码" />
            <node content-desc="甲充电站" clickable="true" />
            <node content-desc="乙汽车充电站" clickable="true" />
            <node content-desc="丙重卡充电站" clickable="true" />
        </hierarchy>"""
        assessment = assess_page(xml)
        self.assertEqual(PageKind.SEARCH_RESULTS, assessment.kind)

    def test_detail_requires_strong_detail_markers(self):
        xml = """<hierarchy>
            <node content-desc="测试重卡充电站" />
            <node text="营业时间" />
            <node text="电站信息" />
            <node text="地图" />
            <node text="电话" />
            <node text="导航" />
            <node text="路线" />
        </hierarchy>"""
        assessment = assess_page(xml, "测试重卡充电站")
        self.assertEqual(PageKind.DETAIL, assessment.kind)
        self.assertTrue(assessment.expected_station_visible)

    def test_basic_detail_without_phone_or_station_info(self):
        xml = """<hierarchy>
            <node content-desc="汽车充电站(测试重卡充电站)" />
            <node text="暂无营业时间" />
            <node text="驾车88.3公里" />
            <node text="地图" />
            <node text="导航" />
            <node text="路线" />
        </hierarchy>"""
        assessment = assess_page(xml, "汽车充电站(测试重卡充电站)")
        self.assertEqual(PageKind.DETAIL, assessment.kind)
        self.assertTrue(assessment.expected_station_visible)

    def test_compact_detail_actions_are_detail_page(self):
        xml = """<hierarchy>
            <node content-desc="陇海路临湖路重卡汽车充电站" clickable="true" />
            <node text="营业中" />
            <node text="24小时营业" />
            <node text="详情" />
            <node text="地图" />
            <node text="导航" />
            <node text="路线" />
        </hierarchy>"""

        assessment = assess_page(xml, "陇海路临湖路重卡汽车充电站")

        self.assertEqual(PageKind.DETAIL, assessment.kind)
        self.assertIn("compact_detail_actions", assessment.reasons)

    def test_poi_summary_card_is_detail_page(self):
        xml = """<hierarchy>
            <node content-desc="展开列表" />
            <node content-desc="云快充汽车充电站(畅行重卡2站)" />
            <node text="刚刚浏览" />
            <node text="距云快充汽车充电站(畅行重卡2站)·地上｜昆仑能源西北侧" />
            <node text="暂无更多内容" />
            <node content-desc="搜索框，云快充汽车充电站(畅行重卡2站)" />
        </hierarchy>"""

        assessment = assess_page(xml, "云快充汽车充电站(畅行重卡2站)")

        self.assertEqual(PageKind.DETAIL, assessment.kind)
        self.assertIn("poi_summary_card", assessment.reasons)
        self.assertTrue(assessment.expected_station_visible)

    def test_price_detail_page(self):
        xml = """<hierarchy>
            <node text="00:00-07:00" />
            <node text="07:00-16:00" />
            <node text="参考价" />
            <node text="电费" />
            <node text="服务费" />
        </hierarchy>"""
        self.assertEqual(PageKind.PRICE_DETAIL, assess_page(xml).kind)

    def test_live_search_and_detail_fixtures_when_available(self):
        fixture_dir = Path(__file__).parent / "debug_runs" / "page_state_compare"
        search_path = fixture_dir / "search.xml"
        detail_path = fixture_dir / "detail.xml"
        if not search_path.exists() or not detail_path.exists():
            self.skipTest("live fixtures unavailable")
        self.assertEqual(
            PageKind.SEARCH_RESULTS,
            assess_page(search_path.read_text(encoding="utf-8")).kind,
        )
        self.assertEqual(
            PageKind.DETAIL,
            assess_page(detail_path.read_text(encoding="utf-8")).kind,
        )

    def test_station_name_normalization(self):
        self.assertEqual(
            normalize_station_name("特来电汽车充电站（测试站）"),
            normalize_station_name("特来电充电站(测试站)"),
        )


if __name__ == "__main__":
    unittest.main()
