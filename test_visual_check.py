import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from visual_check import (
    PageState,
    QianwenVisionAdapter,
    VisualChecker,
    integrate_with_crawler,
)


class FakeDevice:
    def __init__(self, xml_text):
        self.xml_text = xml_text
        self.screenshot_calls = 0

    def dump_hierarchy(self):
        return self.xml_text

    def screenshot(self, filepath):
        self.screenshot_calls += 1
        Path(filepath).write_bytes(b"fake")


class VisualCheckerTests(unittest.TestCase):
    def test_xml_fallback_uses_shared_page_assessment_without_screenshot(self):
        xml = """<hierarchy>
            <node content-desc="甲充电站" clickable="true" />
            <node content-desc="乙充电站" clickable="true" />
        </hierarchy>"""
        device = FakeDevice(xml)
        with tempfile.TemporaryDirectory() as temp_dir:
            checker = VisualChecker(device, screenshot_dir=temp_dir)
            state, info = checker.check(use_visual=False)

        self.assertEqual(PageState.SEARCH_RESULTS, state)
        self.assertTrue(info["fallback"])
        self.assertEqual(0, device.screenshot_calls)

    def test_visual_api_error_falls_back_to_xml(self):
        xml = """<hierarchy>
            <node content-desc="测试充电站" />
            <node text="电站信息" />
            <node text="营业时间" />
        </hierarchy>"""
        device = FakeDevice(xml)
        visual_result = {
            "page_type": "other",
            "has_popup": False,
            "is_normal": True,
            "_error": "service unavailable",
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            checker = VisualChecker(
                device,
                visual_model_func=lambda path: visual_result,
                screenshot_dir=temp_dir,
            )
            state, info = checker.check(use_visual=True)

        self.assertEqual(PageState.DETAIL_PAGE, state)
        self.assertTrue(info["fallback"])
        self.assertEqual("service unavailable", info["visual_error"])
        self.assertEqual(1, device.screenshot_calls)

    def test_recognized_visual_popup_is_used(self):
        device = FakeDevice("<hierarchy />")
        visual_result = {
            "page_type": "popup",
            "has_popup": True,
            "popup_description": "permission",
            "is_normal": False,
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            checker = VisualChecker(
                device,
                visual_model_func=lambda path: visual_result,
                screenshot_dir=temp_dir,
            )
            state, info = checker.check(use_visual=True)

        self.assertEqual(PageState.POPUP_BLOCKING, state)
        self.assertNotIn("fallback", info)

    def test_integration_sets_checker_without_wrapping_collection(self):
        class FakeCrawler:
            def __init__(self):
                self.d = FakeDevice("<hierarchy />")
                self.visual_checker = None

            def collect_detail(self, station, city):
                return station, city

        crawler = FakeCrawler()
        original_collect = crawler.collect_detail
        with tempfile.TemporaryDirectory() as temp_dir:
            checker = integrate_with_crawler(crawler)
            checker.screenshot_dir = temp_dir

        self.assertIs(crawler.visual_checker, checker)
        self.assertEqual(original_collect, crawler.collect_detail)

    def test_missing_api_key_is_an_error_not_a_normal_page(self):
        with patch.dict(os.environ, {"DASHSCOPE_API_KEY": ""}):
            result = QianwenVisionAdapter(api_key="")("unused.png")

        self.assertFalse(result["is_normal"])
        self.assertIn("_error", result)


if __name__ == "__main__":
    unittest.main()
