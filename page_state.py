import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from enum import Enum
from typing import Optional, Tuple


class PageKind(Enum):
    HOME = "home"
    SEARCH_RESULTS = "search_results"
    DETAIL = "detail"
    PRICE_DETAIL = "price_detail"
    POPUP = "popup"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class PageAssessment:
    kind: PageKind
    confidence: float
    reasons: Tuple[str, ...]
    expected_station_visible: bool = False


def normalize_station_name(value: str) -> str:
    normalized = re.sub(r"[\s（）()·•\-—_]+", "", value or "")
    normalized = normalized.replace("汽车充电站", "充电站")
    normalized = normalized.replace("超级充电站", "充电站")
    return normalized.lower()


def _extract_values(xml_text: str):
    texts = []
    descriptions = []
    clickable_descriptions = []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return texts, descriptions, clickable_descriptions

    for node in root.iter("node"):
        text = node.attrib.get("text", "").strip()
        description = node.attrib.get("content-desc", "").strip()
        if text:
            texts.append(text)
        if description:
            descriptions.append(description)
            if node.attrib.get("clickable") == "true":
                clickable_descriptions.append(description)
    return texts, descriptions, clickable_descriptions


def assess_page(xml_text: str, expected_station: Optional[str] = None) -> PageAssessment:
    texts, descriptions, clickable_descriptions = _extract_values(xml_text)
    combined_values = texts + descriptions
    combined_text = "\n".join(combined_values)
    reasons = []

    expected_visible = False
    if expected_station:
        expected_name = normalize_station_name(expected_station)
        expected_visible = any(
            expected_name and (
                expected_name in normalize_station_name(value)
                or normalize_station_name(value) in expected_name
            )
            for value in combined_values
            if len(normalize_station_name(value)) >= 4
        )

    popup_keywords = (
        "仅在使用中允许",
        "使用应用时允许",
        "始终允许",
        "暂不更新",
        "以后再说",
        "我知道了",
    )
    if any(keyword in combined_text for keyword in popup_keywords):
        reasons.append("popup_keyword")
        return PageAssessment(PageKind.POPUP, 0.9, tuple(reasons), expected_visible)

    time_period_count = len(re.findall(r"\d{2}:\d{2}[-~]\d{2}:\d{2}", combined_text))
    if time_period_count >= 2 and "参考价" in combined_text and "服务费" in combined_text:
        reasons.extend(("multiple_time_periods", "fee_breakdown"))
        return PageAssessment(PageKind.PRICE_DETAIL, 0.98, tuple(reasons), expected_visible)

    detail_score = 0
    if "电站信息" in combined_text:
        detail_score += 5
        reasons.append("station_info_section")
    if "营业时间" in combined_text:
        detail_score += 2
        reasons.append("business_hours")
    if "24小时营业" in combined_text or "营业中" in combined_text:
        detail_score += 2
        reasons.append("business_status")
    if "24小时价格趋势图" in combined_text:
        detail_score += 2
        reasons.append("price_trend")
    if "扫码" in combined_text:
        detail_score += 1
        reasons.append("scan_action")
    if "地图" in texts and "电话" in texts:
        detail_score += 2
        reasons.append("map_phone_actions")
    elif "地图" in texts:
        detail_score += 1
        reasons.append("map_action")
    if "导航" in texts and "路线" in texts:
        detail_score += 1
        reasons.append("navigation_actions")
    compact_detail_actions = (
        expected_visible
        and "详情" in texts
        and "地图" in texts
        and "导航" in texts
        and "路线" in texts
    )
    if compact_detail_actions:
        reasons.append("compact_detail_actions")
        return PageAssessment(PageKind.DETAIL, 0.93, tuple(reasons), expected_visible)
    if any("驾车" in value and "公里" in value for value in combined_values):
        detail_score += 2
        reasons.append("driving_summary")
    if expected_visible:
        detail_score += 1
        reasons.append("expected_station_visible")

    search_card_count = sum(
        1
        for description in clickable_descriptions
        if "充电" in description and not description.startswith("搜索框")
    )
    search_score = 0
    if "在此区域搜索" in combined_text:
        search_score += 5
        reasons.append("search_area_action")
    if search_card_count >= 2:
        # The area-search action scrolls off-screen; multiple station cards remain.
        search_score += 5
        reasons.append("multiple_search_cards")
    elif search_card_count:
        search_score += 1
        reasons.append(f"search_cards:{search_card_count}")

    poi_summary_card = (
        "展开列表" in combined_text
        and "暂无更多内容" in combined_text
        and "在此区域搜索" not in combined_text
        and search_card_count <= 1
        and (
            "刚刚浏览" in combined_text
            or re.search(r"\d+人浏览", combined_text)
            or "停车费" in combined_text
            or any(
                "充电" in value and not value.startswith("搜索框")
                for value in combined_values
            )
        )
    )
    if poi_summary_card:
        reasons.append("poi_summary_card")
        return PageAssessment(PageKind.DETAIL, 0.92, tuple(reasons), expected_visible)

    if detail_score >= 5 and "在此区域搜索" not in combined_text:
        confidence = min(0.99, 0.72 + detail_score * 0.035)
        return PageAssessment(PageKind.DETAIL, confidence, tuple(reasons), expected_visible)

    if search_score >= 5:
        confidence = min(0.99, 0.75 + search_score * 0.025)
        return PageAssessment(PageKind.SEARCH_RESULTS, confidence, tuple(reasons), expected_visible)

    if "maphome_searchbar_bg" in xml_text or (
        "首页" in texts and "打车" in texts and "我的" in texts
    ):
        reasons.append("home_navigation")
        return PageAssessment(PageKind.HOME, 0.9, tuple(reasons), expected_visible)

    return PageAssessment(PageKind.UNKNOWN, 0.2, tuple(reasons), expected_visible)


def is_detail_page(xml_text: str, expected_station: Optional[str] = None) -> bool:
    return assess_page(xml_text, expected_station).kind == PageKind.DETAIL


def is_search_results_page(xml_text: str) -> bool:
    return assess_page(xml_text).kind == PageKind.SEARCH_RESULTS
