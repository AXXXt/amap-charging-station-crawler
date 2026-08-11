"""
crawler.py — 高德地图重卡充电站数据采集引擎
功能：
  1. 按城市/关键词搜索充电站
  2. 逐个进入详情页采集数据
  3. 两次dump合并（初始+滚动）确保完整数据
  4. 高德API逆地理编码获取经纬度
"""
import uiautomator2 as u2
import time
import re
import json
import os
import subprocess
import xml.etree.ElementTree as ET
import requests
from datetime import datetime, timezone
from urllib.parse import urlencode
from page_state import PageKind, assess_page, normalize_station_name

# ============================================================
# CONFIG
# ============================================================
DEVICE_SERIAL = os.getenv("DEVICE_SERIAL", "RFCXA0W194D")
ADB_PATH = os.getenv(
    "ADB_PATH",
    r"C:\Users\26381\AppData\Local\Android\Sdk\platform-tools\adb.exe",
)
AMAP_PACKAGE = "com.autonavi.minimap"
AMAP_API_KEY = os.getenv("AMAP_API_KEY", "")
SEARCH_KEYWORD = "重卡充电站"
# 河南省各市及区县（按区县粒度搜索，避免遗漏）
CITY_DISTRICTS = {
    "郑州": ["中原区", "二七区", "管城回族区", "金水区", "上街区", "惠济区",
             "中牟县", "巩义市", "荥阳市", "新密市", "新郑市", "登封市"],
    "洛阳": ["老城区", "西工区", "瀍河区", "涧西区", "偃师区", "孟津区",
             "洛龙区", "新安县", "栾川县", "嵩县", "汝阳县", "宜阳县",
             "洛宁县", "伊川县"],
    "开封": ["龙亭区", "顺河回族区", "鼓楼区", "禹王台区", "祥符区",
             "杞县", "通许县", "尉氏县", "兰考县"],
    "南阳": ["宛城区", "卧龙区", "南召县", "方城县", "西峡县", "镇平县",
             "内乡县", "淅川县", "社旗县", "唐河县", "新野县", "桐柏县", "邓州市"],
    "许昌": ["魏都区", "建安区", "鄢陵县", "襄城县", "禹州市", "长葛市"],
    "平顶山": ["新华区", "卫东区", "石龙区", "湛河区", "宝丰县", "叶县",
               "鲁山县", "郏县", "舞钢市", "汝州市"],
    "新乡": ["红旗区", "卫滨区", "凤泉区", "牧野区", "新乡县", "获嘉县",
             "原阳县", "延津县", "封丘县", "卫辉市", "辉县市", "长垣市"],
    "安阳": ["文峰区", "北关区", "殷都区", "龙安区", "安阳县", "汤阴县",
             "滑县", "内黄县", "林州市"],
    "焦作": ["解放区", "中站区", "马村区", "山阳区", "修武县", "博爱县",
             "武陟县", "温县", "沁阳市", "孟州市"],
    "商丘": ["梁园区", "睢阳区", "民权县", "睢县", "宁陵县", "柘城县",
             "虞城县", "夏邑县", "永城市"],
    "周口": ["川汇区", "淮阳区", "扶沟县", "西华县", "商水县", "沈丘县",
             "郸城县", "太康县", "鹿邑县", "项城市"],
    "驻马店": ["驿城区", "西平县", "上蔡县", "平舆县", "正阳县", "确山县",
               "泌阳县", "汝南县", "遂平县", "新蔡县"],
    "信阳": ["浉河区", "平桥区", "罗山县", "光山县", "新县", "商城县",
             "固始县", "潢川县", "淮滨县", "息县"],
    "漯河": ["源汇区", "郾城区", "召陵区", "舞阳县", "临颍县"],
    "三门峡": ["湖滨区", "陕州区", "渑池县", "卢氏县", "义马市", "灵宝市"],
    "鹤壁": ["鹤山区", "山城区", "淇滨区", "浚县", "淇县"],
    "濮阳": ["华龙区", "清丰县", "南乐县", "范县", "台前县", "濮阳县"],
    "济源": ["济源"],  # 省直辖县级市，只有一个区划
}

# 从 CITY_DISTRICTS 生成所有搜索组合：(城市, 区县/全市)
def generate_search_queries():
    """生成所有搜索组合：每个区县 + 每个城市兜底"""
    queries = []
    for city, districts in CITY_DISTRICTS.items():
        for district in districts:
            # 跳过区县名等于城市名的（如济源）
            if district == city:
                queries.append((city, f"{city}{SEARCH_KEYWORD}"))
            else:
                queries.append((city, f"{city}{district}{SEARCH_KEYWORD}"))
        # 全市兜底搜索（防止区县搜索有遗漏）
        queries.append((city, f"{city}{SEARCH_KEYWORD}"))
    return queries
SCROLL_SMALL = (540, 2000, 540, 1600)  # 小幅度滚动用于露出时间段

# ============================================================
# PAGE TYPE CLASSIFIER
# ============================================================
# UI结构特征（非数据字段，仅用于判断页面类型）
PAGE_STRUCTURE_MARKERS = {
    "has_equipment":     ["空闲", "空"],                       # 枪数/功率行
    "has_price_trend":   ["24小时价格趋势图"],                  # 内嵌价格趋势图区块
    "has_price_click":   ["00:00起降至", "查看"],              # 需要点击电价跳转分时页
    "has_parking":       ["停车费", "停车免费"],                # 停车费行
    "has_occupancy":     ["占位费"],                           # 占位费行
    "has_business_hours":["营业时间"],                          # 营业时间行
    "has_facilities":    ["卫生间", "休息室", "便利店", "重卡车位"], # 设施标签行
    "has_price_section": ["/度", "￥"],                        # 电价展示区
}

def classify_detail_page(xml_text):
    """
    纯UI结构判断，不依赖运营商等数据字段
    根据初始dump中实际出现的UI区块识别详情页类型
    
    Returns: {
        "type": "basic|standard|full_trend",
        "features": {...},
        "scrolls_needed": 0|1|2,
        "description": "..."
    }
    """
    features = {}
    for name, keywords in PAGE_STRUCTURE_MARKERS.items():
        features[name] = any(kw in xml_text for kw in keywords)
    
    # 分类逻辑：只看UI区块是否存在
    if features["has_price_trend"]:
        # 有内嵌趋势图 = 信息最全，需要2次滚动
        page_type = "full_trend"
        scrolls = 2
        desc = "完整：含24h价格趋势图"
    elif features["has_price_click"]:
        # 需要点击电价跳转分时页 = 特殊类型，滚1次 + 点击价格
        page_type = "click_to_expand"
        scrolls = 1
        desc = "需点击电价查看分时详情"
    elif features["has_equipment"]:
        # 有设备区块但没有趋势图 = 中等，需1次滚动
        page_type = "standard"
        scrolls = 1
        desc = "标准：含设备信息"
    elif features["has_parking"] or features["has_occupancy"]:
        # 只有停车/占位费信息，1次滚动看看下面有没有设备
        page_type = "standard"
        scrolls = 1
        desc = "标准：含停车信息"
    else:
        # 什么都没有 = 基础页面，不滚动
        page_type = "basic"
        scrolls = 0
        desc = "基础：仅名称和地址"
    
    return {
        "type": page_type,
        "features": features,
        "scrolls_needed": scrolls,
        "description": desc
    }



# ============================================================
# XML PARSER (from extract_v3.py)
# ============================================================
def parse_detail_xml(xml_text):
    """从XML文本提取结构化数据"""
    root = ET.fromstring(xml_text)
    
    result = {
        "station_name": "", "tags": [], "category": "", "facilities": [],
        "business_hours": "", "distance": "", "duration": "", "address": "",
        "operator": "", "current_price": "", "parking_fee": "", "occupancy_fee": "",
        "favorite_count": "", "view_count": "",
        "fast_available": "", "fast_total": "", "fast_power": "",
        "super_available": "", "super_total": "", "super_power": "",
        "slow_available": "", "slow_total": "", "slow_power": "",
        "price_trend_title": "", "fast_prices": [], "slow_prices": [],
    }
    
    all_nodes = []
    def walk(node):
        all_nodes.append({
            "text": node.attrib.get("text", "").strip(),
            "desc": node.attrib.get("content-desc", "").strip(),
        })
        for child in node:
            walk(child)
    walk(root)
    
    texts = [n["text"] for n in all_nodes if n["text"]]
    
    # Station name
    for n in all_nodes:
        station_name_markers = (
            "充电站", "充换电站", "超充站", "快充站", "重卡站",
            "充电场", "充电中心",
        )
        if n["desc"] and any(marker in n["desc"] for marker in station_name_markers):
            result["station_name"] = n["desc"]; break
    
    # Tags (dedup)
    result["tags"] = list(dict.fromkeys(
        t for t in texts if t in ["优选电站", "刚刚浏览", "新站", "热门"]
    ))
    
    # Category
    if "充电站" in texts: result["category"] = "充电站"
    
    # Facilities (dedup with dict to preserve order)
    facility_set = [
        "地上", "地面", "地下", "卫生间", "休息室", "便利店",
        "重卡车位", "桩多", "免费停车", "WIFI", "无障碍"
    ]
    result["facilities"] = list(dict.fromkeys(
        facility
        for text in texts
        for facility in facility_set
        if facility in text
    ))
    
    # Business hours
    for i, t in enumerate(texts):
        if t == "营业时间" and i + 1 < len(texts):
            result["business_hours"] = texts[i + 1]; break
        if "暂无营业时间" in t:
            result["business_hours"] = "暂无营业时间"; break
        if "24小时营业" in t:
            result["business_hours"] = t; break
        if "营业中" in t:
            if i + 1 < len(texts) and "小时营业" in texts[i + 1]:
                result["business_hours"] = texts[i + 1]
            else:
                result["business_hours"] = t
            break
    
    # Distance & duration
    for t in texts:
        if "驾车" in t and "公里" in t: result["distance"] = t
        if "分钟" in t and len(t) <= 5:
            try:
                int(t.replace("分钟", "").strip())
                result["duration"] = t
            except: pass
    
    # Address
    for t in texts:
        address_markers = ("省", "市", "县", "区", "镇", "乡", "村", "路", "街", "道", "号", "高速")
        if len(t) >= 6 and any(marker in t for marker in address_markers):
            if t.startswith("停车费") or t.startswith("占位费"): continue
            if any(bad in t for bad in ["通知", "K/s", "正在充电", "WLAN", "已选中", "未选中", "信号"]): continue
            parts = [part.strip() for part in re.split(r"[|｜]", t) if part.strip()]
            while parts and parts[0] in facility_set:
                parts.pop(0)
            result["address"] = " | ".join(parts) if parts else t
            break
    
    # Operator
    known_operators = [
        "特来电", "新电途", "星星充电", "国家电网", "南方电网",
        "依威能源", "云快充", "万马爱充", "小桔充电", "快电", "蔚来",
    ]
    for text in texts:
        if text in known_operators:
            result["operator"] = text
            break
    if not result["operator"] and result["station_name"]:
        for operator in known_operators:
            if operator in result["station_name"]:
                result["operator"] = operator
                break
    
    # Current price
    for i, t in enumerate(texts):
        if t == "/度" and i > 0:
            p = texts[i - 1]
            if p.replace(".", "").replace("￥", "").isdigit():
                result["current_price"] = p; break
    
    # Parking fee
    for t in texts:
        if t.startswith("停车费 "): result["parking_fee"] = t[4:].lstrip("：:"); break
        if t.startswith("停车费"): result["parking_fee"] = t[3:].lstrip("：:"); break
        if "停车免费" in t: result["parking_fee"] = "免费"; break
        if "停车费" in t:
            # Extract from format like "停车费：免费"
            idx = t.find("停车费")
            result["parking_fee"] = t[idx+3:].lstrip("：:").strip()
            break
    
    # Occupancy fee
    for t in texts:
        if "占位费" in t: result["occupancy_fee"] = t; break
    
    # Favorite count
    for i, t in enumerate(texts):
        if t == "分享" and i > 0 and texts[i-1].isdigit():
            result["favorite_count"] = texts[i-1]; break
    
    # View count
    for i, t in enumerate(texts):
        if t == "浏览" and i > 0 and texts[i-1].isdigit():
            result["view_count"] = texts[i-1]; break
    
    # Equipment (matches "空闲" and abbreviated "空")
    for i, t in enumerate(texts):
        if t in ("空闲", "空") and i > 0:
            ctype = texts[i-1]
            try:
                avail = texts[i+1]; total = texts[i+2].replace("/", "")
                power = ""
                for j in range(i+3, min(i+6, len(texts))):
                    if "kW" in texts[j]: power = texts[j]; break
                if "超充" in ctype:
                    result["super_available"], result["super_total"], result["super_power"] = avail, total, power
                elif "快充" in ctype:
                    result["fast_available"], result["fast_total"], result["fast_power"] = avail, total, power
                elif "慢充" in ctype:
                    result["slow_available"], result["slow_total"], result["slow_power"] = avail, total, power
            except: pass
    
    # 24h price trend
    if "24小时价格趋势图" in texts:
        result["price_trend_title"] = "24小时价格趋势图"
        prices = []
        for i, t in enumerate(texts):
            if t.endswith("/度") and t.startswith("￥"):
                pv = t.replace("￥", "").replace("/度", "")
                tr = ""
                for j in range(i, min(len(texts), i+4)):
                    c = texts[j]
                    if ("-" in c and ":" in c) or c == "当前时段": tr = c; break
                prices.append({"price": pv, "time": tr})
        
        if "快充价格" in texts and "慢充价格" in texts:
            mid = len(prices) // 2
            result["fast_prices"] = prices[:mid]
            result["slow_prices"] = prices[mid:]
        else:
            result["fast_prices"] = prices
    
    return result



def parse_price_detail_page(xml_text):
    """
    解析点击跳转后的"充电价格详情"页
    状态机按文档顺序解析：时间段 → 参考价 → 电费 → 服务费
    Returns: list of {time, total_price, elec_fee, service_fee, tag}
    """
    import re
    
    texts = []
    for m in re.finditer(r'text="([^"]*)"', xml_text):
        t = m.group(1).strip()
        if t:
            texts.append(t)
    
    STATE_IDLE, STATE_REF, STATE_ELEC, STATE_SVC = 0, 1, 2, 3
    
    periods = []
    current = None
    state = STATE_IDLE
    int_buffer = ""  # 整数部分缓冲（处理价格拆分为"1" + ".45"的情况）
    
    for t in texts:
        # 时间段标记
        if re.match(r'^\d{2}:\d{2}[-~]\d{2}:\d{2}$', t):
            if current:
                periods.append(current)
            current = {"time": t, "total_price": "", "elec_fee": "", "service_fee": "", "tag": ""}
            state = STATE_IDLE
            int_buffer = ""
            continue
        
        if current is None:
            continue
        
        # 标签
        if t in ("最低", "当前计费时段"):
            current["tag"] = t
            continue
        
        # 整数价格缓冲：纯数字（如"1"在".45"前面）
        if t.isdigit() and len(t) <= 2:
            int_buffer = t
            continue
        
        # 状态切换
        if "参考价" in t:
            state = STATE_REF; int_buffer = ""; continue
        if "电费:" in t or "电费" in t:
            state = STATE_ELEC; int_buffer = ""; continue
        if "服务费:" in t or "服务费" in t:
            state = STATE_SVC; int_buffer = ""; continue
        
        # 小数价格：".XX"
        if re.match(r'^\.\d+$', t):
            price = (int_buffer if int_buffer else "0") + t
            if state == STATE_REF:
                current["total_price"] = price
            elif state == STATE_ELEC:
                current["elec_fee"] = price
            elif state == STATE_SVC:
                current["service_fee"] = price
            state = STATE_IDLE
            int_buffer = ""
            continue
        
        # 完整价格："X.XX"
        if re.match(r'^\d+\.\d+$', t):
            if state == STATE_REF:
                current["total_price"] = t
            elif state == STATE_ELEC:
                current["elec_fee"] = t
            elif state == STATE_SVC:
                current["service_fee"] = t
            state = STATE_IDLE
            continue
    
    if current:
        periods.append(current)
    
    return periods

def merge_results(r1, r2):
    """合并两次dump结果，r2补充r1缺失的字段（但不覆盖已有字段）"""
    merged = r1.copy()
    # Blacklist: fields that should NEVER be overwritten from scroll dumps
    # (scrolling may show nearby stations' names/addresses)
    NEVER_OVERWRITE = ["station_name", "address"]
    
    for key in merged:
        if not merged[key] and r2.get(key) and key not in NEVER_OVERWRITE:
            merged[key] = r2[key]
    
    # Special merge for lists: combine + dedup
    for list_key in ["tags", "facilities"]:
        if r2.get(list_key):
            seen = set(merged[list_key])
            for item in r2[list_key]:
                if item not in seen:
                    merged[list_key].append(item)
                    seen.add(item)
    
    # For prices: r2 may have time ranges that r1 doesn't
    for price_key in ["fast_prices", "slow_prices"]:
        r1_has_time = any(p.get("time") for p in merged.get(price_key, []))
        if not r1_has_time and r2.get(price_key):
            merged[price_key] = r2[price_key]
    
    return merged


# ============================================================
# AMAP GEOCODING
# ============================================================
def geocode(address, city="郑州"):
    """高德地理编码：地址 → 经纬度"""
    import re

    if not AMAP_API_KEY:
        return None
    
    # 从地址中提取城市名（如"河南省洛阳市偃师区..."）
    city_match = re.search(r'([^省]+市)', address)
    if city_match:
        city = city_match.group(1)
    
    url = "https://restapi.amap.com/v3/geocode/geo"
    try:
        resp = requests.get(url, params={
            "key": AMAP_API_KEY, "address": address,
            "city": city, "output": "JSON"
        }, timeout=10)
        data = resp.json()
        if data.get("status") == "1" and data.get("geocodes"):
            loc = data["geocodes"][0].get("location", "")
            if loc:
                lng, lat = loc.split(",")
                return {"longitude": float(lng), "latitude": float(lat), "source": "amap_geocode"}
        else:
            # 重试：不加city参数
            resp2 = requests.get(url, params={
                "key": AMAP_API_KEY, "address": address,
                "output": "JSON"
            }, timeout=10)
            data2 = resp2.json()
            if data2.get("status") == "1" and data2.get("geocodes"):
                loc = data2["geocodes"][0].get("location", "")
                if loc:
                    lng, lat = loc.split(",")
                    return {"longitude": float(lng), "latitude": float(lat), "source": "amap_geocode"}
    except Exception as e:
        print(f"    [!] Geocode error: {e}")
    return None


# ============================================================
# DEVICE CONTROL
# ============================================================
class AmapCrawler:
    def __init__(self, serial=DEVICE_SERIAL, visual_checker=None, stop_event=None):
        self.serial = serial
        self.d = u2.connect(serial)
        self.results = []
        self.visual_checker = visual_checker  # 可选视觉自检器
        self.stop_event = stop_event
    


    def _should_stop(self):
        return self.stop_event is not None and self.stop_event.is_set()

    def _ensure_amap_foreground(self):
        current = self.d.app_current()
        if current.get("package") == AMAP_PACKAGE:
            return
        self.d.app_start(AMAP_PACKAGE, stop=False)
        if not self.d.app_wait(AMAP_PACKAGE, timeout=10):
            raise RuntimeError("高德地图启动失败")
        time.sleep(3)

    def _wait_for_page(self, expected_kind, expected_station=None, timeout=6):
        deadline = time.monotonic() + timeout
        last_xml = ""
        last_assessment = None
        while time.monotonic() < deadline:
            if self._should_stop():
                return False, last_assessment, last_xml
            last_xml = self.d.dump_hierarchy()
            last_assessment = assess_page(last_xml, expected_station)
            station_matches = not expected_station or last_assessment.expected_station_visible
            if last_assessment.kind == expected_kind and station_matches:
                return True, last_assessment, last_xml
            time.sleep(0.6)
        return False, last_assessment, last_xml

    def _confirm_detail_page(self, station_name):
        confirmed, assessment, _ = self._wait_for_page(
            PageKind.DETAIL,
            expected_station=station_name,
            timeout=6,
        )
        if confirmed:
            return True

        if self._should_stop():
            return False

        if self.visual_checker:
            try:
                state, info = self.visual_checker.check(use_visual=True)
                visual_result = info.get("visual_result", {})
                print(
                    f"      [视觉] 详情转场异常: xml={assessment.kind.value if assessment else 'unknown'}, "
                    f"visual={state.name}"
                )
                if state.name == "POPUP_BLOCKING":
                    print(f"      [视觉] 弹窗: {visual_result.get('popup_description', '')}")
                    self.visual_checker.recover()
                    confirmed, _, _ = self._wait_for_page(
                        PageKind.DETAIL,
                        expected_station=station_name,
                        timeout=5,
                    )
                    return confirmed
            except Exception as error:
                print(f"      [视觉] 详情确认失败: {error}")
        return False

    def _open_poi_detail(self, station):
        poi_id = (station.get("id") or "").strip()
        if not poi_id:
            return False

        params = {
            "poiname": station.get("name", ""),
            "poiid": poi_id,
        }
        if station.get("latitude") not in (None, ""):
            params["lat"] = station["latitude"]
        if station.get("longitude") not in (None, ""):
            params["lon"] = station["longitude"]

        uri = f"amapuri://poi/detail?{urlencode(params)}"
        remote_command = (
            "am start -W -a android.intent.action.VIEW "
            "-c android.intent.category.DEFAULT "
            f"-d '{uri}' -p {AMAP_PACKAGE}"
        )
        try:
            completed = subprocess.run(
                [ADB_PATH, "-s", self.serial, "shell", remote_command],
                capture_output=True,
                text=True,
                timeout=20,
            )
        except Exception as error:
            print(f"      [POI] 唤起失败: {error}")
            return False

        if completed.returncode != 0 or "Error:" in completed.stdout:
            message = (completed.stderr or completed.stdout).strip()
            print(f"      [POI] 唤起失败: {message[:120]}")
            return False

        confirmed, assessment, _ = self._wait_for_page(
            PageKind.DETAIL,
            expected_station=station.get("name"),
            timeout=8,
        )
        if confirmed:
            return True

        print(
            f"      [POI] 未进入详情页: "
            f"{assessment.kind.value if assessment else 'unknown'}"
        )
        return False

    def _recover_to_search_results(self):
        for _ in range(3):
            current = self.d.app_current()
            if current.get("package") != AMAP_PACKAGE:
                self._ensure_amap_foreground()
                return False
            xml = self.d.dump_hierarchy()
            if assess_page(xml).kind == PageKind.SEARCH_RESULTS:
                return True
            self.d.press("back")
            time.sleep(1.5)
        return assess_page(self.d.dump_hierarchy()).kind == PageKind.SEARCH_RESULTS

    @staticmethod
    def _extract_visible_station_cards(xml_text):
        cards = []
        try:
            root = ET.fromstring(xml_text)
        except ET.ParseError:
            return cards

        for node in root.iter("node"):
            if node.attrib.get("clickable") != "true":
                continue
            description = node.attrib.get("content-desc", "").strip()
            if (
                not description
                or "充电" not in description
                or description.startswith("搜索框")
            ):
                continue
            bounds_match = re.match(
                r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]",
                node.attrib.get("bounds", ""),
            )
            if not bounds_match:
                continue
            x1, y1, x2, y2 = map(int, bounds_match.groups())
            cards.append(
                {
                    "name": description,
                    "cx": (x1 + x2) // 2,
                    "cy": (y1 + y2) // 2,
                    "bounds": (x1, y1, x2, y2),
                }
            )
        return sorted(cards, key=lambda card: (card["cy"], card["cx"]))

    def _visible_card_can_be_clicked(self, station):
        x1, y1, x2, y2 = station.get("bounds", (0, 0, 0, 0))
        try:
            _, screen_height = self.d.window_size()
        except Exception:
            screen_height = 2400
        return (
            x2 > x1
            and y2 > y1
            and y1 >= 280
            and station["cy"] <= screen_height - 240
        )

    def _scan_search_results_incrementally(self, query, station_handler, max_scrolls=12):
        seen_keys = set()
        discovered = []
        no_new_count = 0
        scroll_count = 0

        while True:
            if self._should_stop():
                print("    收到停止信号")
                break

            xml = self.d.dump_hierarchy()
            cards = self._extract_visible_station_cards(xml)
            candidate = None
            has_unseen_card = False
            for card in cards:
                key = normalize_station_name(card["name"])
                if not key or key in seen_keys:
                    continue
                has_unseen_card = True
                if self._visible_card_can_be_clicked(card):
                    candidate = (key, card)
                    break

            if candidate:
                key, station = candidate
                seen_keys.add(key)
                station["search_query"] = query
                station["_click_visible"] = True
                discovered.append(dict(station))
                no_new_count = 0
                print(f"    [发现] {station['name'][:50]}，立即进入详情")
                station_handler(station)
                if self._should_stop():
                    print("    收到停止信号")
                    break
                continue

            if not cards or not has_unseen_card:
                no_new_count += 1
            else:
                no_new_count = 0
            if no_new_count >= 2:
                print(f"    滚动{scroll_count}次，无新站点，停止采集")
                break
            if scroll_count >= max_scrolls:
                break

            self.d.swipe(540, 1900, 540, 600, duration=0.4)
            time.sleep(1.5)
            scroll_count += 1

        print(f"    增量发现 {len(discovered)} 个充电站（滚动{scroll_count}次）")
        return discovered

    def _enter_search_query(self, query):
        query_entered = False
        try:
            focused_input = self.d(focused=True)
            if focused_input.exists:
                focused_input.set_text(query)
                query_entered = True
        except Exception:
            pass

        if query_entered:
            return

        try:
            self.d.clear_text()
            time.sleep(0.5)
        except Exception:
            self.d.long_click(540, 176, duration=1.0)
            time.sleep(1)
            xml = self.d.dump_hierarchy()
            if "全选" in xml:
                match = re.search(
                    r'text="全选"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"',
                    xml,
                )
                if match:
                    center_x = (int(match.group(1)) + int(match.group(3))) // 2
                    center_y = (int(match.group(2)) + int(match.group(4))) // 2
                    self.d.click(center_x, center_y)
                    time.sleep(0.5)
            self.d.press("delete")
            time.sleep(0.5)
        self.d.send_keys(query)

    def search_stations(self, city, query=None, recenter_only=False, station_handler=None):
        """搜索指定区县的重卡充电站
        
        Args:
            recenter_only: 仅切换城市定位，不解析站点列表
            station_handler: 发现当前可见站点后立即调用的处理函数
        """
        if self._should_stop():
            return []

        if query is None:
            query = f"{city}{SEARCH_KEYWORD}"
        print(f"\n  [搜索] {query}")
        self._ensure_amap_foreground()
        
        # Smart search bar: detect page state first, then use correct entry
        xml_pre = self.d.dump_hierarchy()
        
        if "maphome_searchbar_bg" in xml_pre:
            # Home page: search bar in middle of screen
            search_bar = self.d(resourceId="com.autonavi.minimap:id/maphome_searchbar_bg")
            if search_bar.exists:
                search_bar.click()
                time.sleep(2)
        elif "com.autonavi.minimap:id/search_text" in xml_pre or "EditText" in xml_pre:
            # Already on search page: tap the search input at top
            search_input = self.d(className="android.widget.EditText")
            if search_input.exists:
                search_input.click()
                time.sleep(1)
        else:
            # Unknown state (e.g. pure map view): try top-right search icon
            # Search icon in map mode is typically at top-right
            self.d.click(723, 149)
            time.sleep(2)
            xml_check = self.d.dump_hierarchy()
            if "EditText" not in xml_check and "搜索" not in xml_check:
                # Last resort: try home search bar position
                self.d.click(540, 1309)
                time.sleep(2)
        
        time.sleep(2)
        
        self._enter_search_query(query)
        time.sleep(1)
        self.d.press("enter")
        time.sleep(3)

        if recenter_only:
            return []

        if station_handler is not None:
            return self._scan_search_results_incrementally(query, station_handler)
        
        # === Scroll and collect ALL search results (waterfall list) ===
        seen_keys = set()
        all_stations = []
        no_new_count = 0
        max_scrolls = 12

        for scroll_idx in range(max_scrolls):
            xml = self.d.dump_hierarchy()

            new_in_scroll = 0
            for m in re.finditer(r'<node[^>]*>', xml):
                node_str = m.group()
                if ('充电站' in node_str or '充电' in node_str) and 'clickable="true"' in node_str:
                    bounds_m = re.search(r'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"', node_str)
                    desc_m = re.search(r'content-desc="([^"]*)"', node_str)
                    if bounds_m:
                        x1, y1, x2, y2 = int(bounds_m.group(1)), int(bounds_m.group(2)), \
                                         int(bounds_m.group(3)), int(bounds_m.group(4))
                        label = desc_m.group(1) if desc_m else ""
                        key = normalize_station_name(label)
                        if key not in seen_keys and "充电" in label and not label.startswith("搜索框"):
                            seen_keys.add(key)
                            all_stations.append({
                                "name": label,
                                "cx": (x1 + x2) // 2,
                                "cy": (y1 + y2) // 2,
                            })
                            new_in_scroll += 1

            if self._should_stop():
                print("    收到停止信号")
                return []
            if new_in_scroll == 0:
                no_new_count += 1
                if no_new_count >= 2:
                    print(f"    滚动{scroll_idx + 1}次，无新站点，停止采集")
                    break
            else:
                no_new_count = 0

            # Scroll down for next batch
            if scroll_idx < max_scrolls - 1:
                self.d.swipe(540, 1900, 540, 600, duration=0.4)
                time.sleep(1.5)

        # Filter + sort by vertical position
        all_stations = [s for s in all_stations if "充电" in s["name"]]
        total_found = len(all_stations)
        print(f"    找到 {total_found} 个充电站（滚动{scroll_idx + 1}次）")
        
        # Scroll back to top so clicks are accurate
        if total_found > 0 and scroll_idx > 0:
            for _ in range(scroll_idx + 2):
                self.d.swipe(540, 400, 540, 1900, duration=0.3)
                time.sleep(0.8)
            time.sleep(1)
            print("    已滚回顶部")
        
        # Visual check: if 0 results, diagnose why
        if total_found == 0 and self.visual_checker:
            print("    [视觉] 搜索无结果，使用视觉模型诊断...")
            try:
                state, vinfo = self.visual_checker.check(use_visual=True)
                if state.name == "POPUP_BLOCKING":
                    print(f"    [视觉] 检测到弹窗: {vinfo.get('visual_result',{}).get('popup_description','')}")
                    self.visual_checker.recover()
                    time.sleep(2)
                else:
                    vr = vinfo.get("visual_result", {})
                    if not vr.get("is_normal", True):
                        print(f"    [视觉] 页面异常: {vr.get('suggestion', '未知')}")
                    else:
                        print(f"    [视觉] 页面状态: {state.name} — 该区县可能确实无重卡充电站")
            except Exception as e:
                print(f"    [视觉] 检查失败: {e}")
        
        return all_stations

    def _open_visible_station(self, station):
        if self._should_stop():
            return False
        center_x = station.get("cx")
        center_y = station.get("cy")
        if center_x is None or center_y is None:
            return False
        print(f"      [定位] 点击当前可见站点: {station['name'][:50]}")
        self.d.click(int(center_x), int(center_y))
        if self._confirm_detail_page(station["name"]):
            return True
        print("      [定位] 点击后未进入目标详情页，停止本次采集")
        self._recover_to_search_results()
        return False
    
    def _find_and_click_station(self, station_name, max_scrolls=12):
        target_name = normalize_station_name(station_name)
        previous_signature = None
        unchanged_count = 0

        for scroll_index in range(max_scrolls + 1):
            if self._should_stop():
                return False

            xml = self.d.dump_hierarchy()
            assessment = assess_page(xml)
            if assessment.kind != PageKind.SEARCH_RESULTS:
                print(f"      [定位] 当前不是搜索结果页: {assessment.kind.value}")
                return False

            best_match = None
            visible_names = []
            for node in ET.fromstring(xml).iter("node"):
                if node.attrib.get("clickable") != "true":
                    continue
                description = node.attrib.get("content-desc", "").strip()
                if "充电" not in description or description.startswith("搜索框"):
                    continue
                visible_names.append(normalize_station_name(description))
                candidate_name = normalize_station_name(description)
                if not candidate_name:
                    continue
                if candidate_name == target_name:
                    score = 1000
                elif target_name in candidate_name:
                    score = 800 + len(target_name)
                elif candidate_name in target_name:
                    score = 700 + len(candidate_name)
                else:
                    continue
                bounds = node.attrib.get("bounds", "")
                bounds_match = re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", bounds)
                if not bounds_match:
                    continue
                x1, y1, x2, y2 = map(int, bounds_match.groups())
                match = (score, (x1 + x2) // 2, (y1 + y2) // 2, description)
                if best_match is None or match[0] > best_match[0]:
                    best_match = match

            if best_match:
                _, center_x, center_y, description = best_match
                print(f"      [定位] 找到候选: {description[:50]}")
                self.d.click(center_x, center_y)
                if self._confirm_detail_page(station_name):
                    return True
                print("      [定位] 点击后未进入目标详情页，停止本次采集")
                self._recover_to_search_results()
                return False

            signature = tuple(sorted(set(visible_names)))
            if signature == previous_signature:
                unchanged_count += 1
                if unchanged_count >= 2:
                    break
            else:
                unchanged_count = 0
                previous_signature = signature

            if scroll_index < max_scrolls:
                self.d.swipe(540, 1900, 540, 650, duration=0.35)
                time.sleep(1.2)

        print(f"      [定位] 未找到目标站点: {station_name[:50]}")
        return False

    def collect_detail(self, station, city):
        """进入详情页采集数据（根据页面类型自适应）"""
        print(f"    [采集] {station['name'][:30]}...")
        
        if self._should_stop():
            return None

        opened_by_poi = False
        opened_from_visible_card = False
        if station.get("_click_visible"):
            opened_from_visible_card = self._open_visible_station(station)
        elif station.get("id"):
            opened_by_poi = self._open_poi_detail(station)

        if (
            not opened_by_poi
            and not opened_from_visible_card
            and not self._find_and_click_station(station["name"])
        ):
            return None
        
        # Dump 1: initial view + classify
        xml1 = self.d.dump_hierarchy()
        detail_assessment = assess_page(xml1, station["name"])
        if detail_assessment.kind != PageKind.DETAIL or (
            not opened_by_poi and not detail_assessment.expected_station_visible
        ):
            print(f"      [采集] 详情页强校验失败: {detail_assessment.kind.value}")
            self._recover_to_search_results()
            return None
        page_info = classify_detail_page(xml1)
        r1 = parse_detail_xml(xml1)
        print(f"      类型: {page_info['type']} ({page_info['description']})")
        
        # Adaptive collection based on page type
        xml2 = xml1  # default: same as xml1
        xml3 = xml1
        price_details = []  # 分时电价详情（click_to_expand类型）
        
        # === Handle click_to_expand FIRST (before scrolling!) ===
        # 必须先点击再滚动，否则坐标会位移
        if page_info["type"] == "click_to_expand":
            m = re.search(r'text="/度"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"', xml1)
            if not m:
                m = re.search(r'text="查看"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"', xml1)
            if m:
                cx = (int(m.group(1)) + int(m.group(3))) // 2
                cy = (int(m.group(2)) + int(m.group(4))) // 2
                self.d.click(cx, cy)
                time.sleep(3)
                xml_price = self.d.dump_hierarchy()
                price_details = parse_price_detail_page(xml_price)
                # 获取跳转页面的运营商
                r_price = parse_detail_xml(xml_price)
                if r_price.get("operator") and not r1.get("operator"):
                    r1["operator"] = r_price["operator"]
                # Back to detail page
                self.d.press("back")
                time.sleep(2)
        
        # Then scroll for other data
        if page_info["scrolls_needed"] >= 1:
            self.d.swipe(*SCROLL_SMALL, duration=0.3)
            time.sleep(2)
            xml2 = self.d.dump_hierarchy()
            r2 = parse_detail_xml(xml2)
        
        if page_info["scrolls_needed"] >= 2:
            self.d.swipe(*SCROLL_SMALL, duration=0.3)
            time.sleep(2)
            xml3 = self.d.dump_hierarchy()
            r3 = parse_detail_xml(xml3)
        
        # Merge all dumps
        if page_info["scrolls_needed"] == 0:
            result = r1
        elif page_info["scrolls_needed"] == 1:
            result = merge_results(r1, r2)
        else:
            result = merge_results(merge_results(r1, r2), r3)

        if (
            not result["station_name"]
            and detail_assessment.expected_station_visible
            and station.get("name")
        ):
            result["station_name"] = station["name"]
            result["station_name_source"] = "input_verified"
        
        # Attach price details from sub-page
        if price_details:
            result["fast_prices"] = price_details
        result["search_city"] = city
        result["search_keyword"] = station.get("search_query", SEARCH_KEYWORD)
        result["collected_at"] = datetime.now(timezone.utc).isoformat()
        result["detail_verified"] = True
        result["name_match"] = detail_assessment.expected_station_visible
        result["match_method"] = "poi_id" if opened_by_poi else "search_card"

        source_address = (station.get("address") or "").strip()
        if result["address"]:
            result["address_source"] = "detail"
        elif source_address:
            result["address"] = source_address
            result["address_source"] = "input"

        if station.get("id"):
            result["source_station_id"] = station["id"]
        result["source_station_name"] = station.get("name", "")
        result["source_address"] = source_address
        
        # Geocode: try address first, then station name, then parenthetical name
        source_longitude = station.get("longitude")
        source_latitude = station.get("latitude")
        if source_longitude not in (None, "") and source_latitude not in (None, ""):
            result["longitude"] = float(source_longitude)
            result["latitude"] = float(source_latitude)
            result["coordinate_source"] = "input"
        else:
            geocode_candidates = []
            if result["address"] and len(result["address"]) > 5:
                geocode_candidates.append(result["address"])
            if result["station_name"]:
                pm = re.search(r'\(([^)]+)\)', result["station_name"])
                if pm:
                    geocode_candidates.append(pm.group(1))
                name = result["station_name"].split("(")[0] if "(" in result["station_name"] else result["station_name"]
                geocode_candidates.append(name)

            for candidate in geocode_candidates:
                geo = geocode(candidate, city)
                if geo:
                    result["longitude"] = geo["longitude"]
                    result["latitude"] = geo["latitude"]
                    result["coordinate_source"] = "amap_geocode"
                    result["geocoded_address"] = candidate
                    break
        
        # Go back to search results
        self.d.press("back")
        time.sleep(2)
        
        return result
    
    def _collect_district_search(self, city, district, query):
        count = 0
        mismatch_count = 0
        attempted_count = 0

        def handle_station(station):
            nonlocal count, mismatch_count, attempted_count
            if self._should_stop():
                return
            attempted_count += 1
            try:
                result = self.collect_detail(station, city)
                if result is None:
                    return

                if district != city:
                    addr = result.get("address", "")
                    name = result.get("station_name", "")
                    if district not in addr and district not in name:
                        mismatch_count += 1
                        print(f"      [{attempted_count}] SKIP: not in {district}")
                        return

                self.results.append(result)
                count += 1
            except Exception as e:
                print(f"      [{attempted_count}] FAIL: {e}")
                try:
                    self._recover_to_search_results()
                except Exception:
                    pass

        stations = self.search_stations(
            city,
            query,
            station_handler=handle_station,
        )
        print(f"    Found {len(stations)} stations in this district")
        return count, mismatch_count

    def run_district(self, city, district):
        print(f"\n  [District] {city}/{district}")

        query = f"{city}{district}{SEARCH_KEYWORD}" if district != city else f"{city}{SEARCH_KEYWORD}"
        self.search_stations(city, query=f"{city}市", recenter_only=True)
        time.sleep(1)

        count, mismatch_count = self._collect_district_search(city, district, query)
        print(f"  District {district} done: {count} stations, skipped {mismatch_count}")
        return count
    
    def run_city(self, city):
        """采集单个城市的全部充电站（按区县遍历）"""
        print(f"\n{'='*50}")
        print(f"  城市: {city}")
        print(f"{'='*50}")
        
        # === 先搜索城市名切换地图中心 ===
        print(f"  [定位] 切换到 {city}...")
        self.search_stations(city, query=f"{city}市", recenter_only=True)
        
        districts = CITY_DISTRICTS.get(city, [city])
        city_total = 0
        
        for district in districts:
            if district == city:
                query = f"{city}{SEARCH_KEYWORD}"
            else:
                query = f"{city}{district}{SEARCH_KEYWORD}"
            
            district_count, _ = self._collect_district_search(city, district, query)
            city_total += district_count
        
        print(f"  城市 {city} 合计: {city_total} 个站点")
        return city_total
    def run_all(self, cities=None):
        """遍历所有城市采集数据（按区县粒度）"""
        if cities is None:
            cities = list(CITY_DISTRICTS.keys())
        
        total = 0
        for city in cities:
            try:
                n = self.run_city(city)
                total += n
            except Exception as e:
                print(f"  [!] 城市 {city} 采集异常: {e}")
        
        # 去重
        self.deduplicate_results()
        
        print(f"\n{'='*50}")
        print(f"  采集完成! 共采集 {len(self.results)} 个去重站点")
        print(f"{'='*50}")
        return self.results
    

    def deduplicate_results(self, distance_threshold=0.5):
        """
        采集后去重：名称相似 + 坐标距离 < threshold (km)
        去重策略：
          1. 完全同名 + 坐标距离 < 500m → 合并（保留数据更全的）
          2. 坐标完全相同 → 合并
          3. 不同名但坐标距离 < 100m → 合并（同一站点不同名称变体）
        """
        import math
        
        def haversine(lon1, lat1, lon2, lat2):
            R = 6371.0
            dlon = math.radians(lon2 - lon1)
            dlat = math.radians(lat2 - lat1)
            a = (math.sin(dlat/2)**2 +
                 math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) *
                 math.sin(dlon/2)**2)
            return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1-a))
        
        print(f"\n  去重前: {len(self.results)} 条")
        
        kept = []
        removed = []
        
        for i, r in enumerate(self.results):
            is_dup = False
            for j, k in enumerate(kept):
                # Both must have coordinates
                if not (r.get("longitude") and r.get("latitude") and
                        k.get("longitude") and k.get("latitude")):
                    continue
                
                dist = haversine(
                    r["longitude"], r["latitude"],
                    k["longitude"], k["latitude"]
                )
                
                same_name = r.get("station_name") == k.get("station_name")
                
                # Same name + close → merge
                if same_name and dist < distance_threshold:
                    is_dup = True
                    # Merge: keep the one with more data fields filled
                    r_filled = sum(1 for v in r.values() if v)
                    k_filled = sum(1 for v in k.values() if v)
                    if r_filled > k_filled:
                        kept[j] = r  # replace with richer data
                    break
                
                # Very close (< 100m) even with different names → merge
                if dist < 0.1:
                    is_dup = True
                    r_filled = sum(1 for v in r.values() if v)
                    k_filled = sum(1 for v in k.values() if v)
                    if r_filled > k_filled:
                        kept[j] = r
                    break
            
            if not is_dup:
                kept.append(r)
            else:
                removed.append(r)
        
        self.results = kept
        print(f"  去重后: {len(self.results)} 条 (移除 {len(removed)} 条重复)")
        
        # Print removed for audit
        for r in removed:
            print(f"    [去重] {r.get('station_name', '?')[:30]} ({r.get('longitude')}, {r.get('latitude')})")
        
        return len(removed)

    def save_results(self, filepath):
        """保存结果到JSON文件"""
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(self.results, f, ensure_ascii=False, indent=2)
        print(f"  结果已保存: {filepath}")


# ============================================================
# MAIN
# ============================================================
if __name__ == "__main__":
    crawler = AmapCrawler()
    
    # Quick test: single city
    print("单城市测试模式...")
    crawler.run_city("郑州")  # 自动按区县遍历 + 全市兜底
    crawler.save_results(r"C:\Users\26381\Desktop\adb-first\collected_data.json")
    
    # Full run (uncomment for production):
    # crawler.run_all()
    # crawler.save_results(r"C:\Users\26381\Desktop\adb-first\collected_data_henan.json")
