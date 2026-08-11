import uiautomator2 as u2
import time, re, json, sys
from datetime import datetime

sys.path.insert(0, r"C:\Users\26381\Desktop\adb-first")
from crawler import (
    parse_detail_xml, merge_results, classify_detail_page,
    parse_price_detail_page, geocode
)

d = u2.connect("RFCXA0W194D")

# 用已知有结果的搜索词
TEST_QUERIES = [
    ("郑州", "郑州重卡充电站"),          # 全市搜索（之前找到4个）
    ("郑州", "郑州惠济区重卡充电站"),     # 区县搜索（之前找到1个）
    ("洛阳", "洛阳重卡充电站"),           # 另一个城市
]

SCROLL_SMALL = (540, 2000, 540, 1600)
all_results = []
seen = set()

for city, query in TEST_QUERIES:
    print(f"\n{'='*50}")
    print(f"  Query: {query}")
    print(f"{'='*50}")
    
    # Navigate to search
    search_bar = d(resourceId="com.autonavi.minimap:id/maphome_searchbar_bg")
    if search_bar.exists:
        search_bar.click()
    else:
        d.click(540, 1309)
    time.sleep(2)
    try: d.clear_text()
    except: pass
    time.sleep(0.5)
    d.send_keys(query); time.sleep(0.5)
    d.press("enter"); time.sleep(3)
    
    # Save XML for debugging
    xml_search = d.dump_hierarchy()
    with open(rf"C:\Users\26381\Desktop\adb-first\batch_search_{city}.xml", "w", encoding="utf-8") as f:
        f.write(xml_search)
    
    # Parse stations
    stations = []
    for m in re.finditer(r'<node[^>]*>', xml_search):
        ns = m.group()
        if 'clickable="true"' in ns:
            bm = re.search(r'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"', ns)
            dm = re.search(r'content-desc="([^"]*)"', ns)
            if bm and dm and "充电" in dm.group(1):
                stations.append({
                    "name": dm.group(1),
                    "x": (int(bm.group(1))+int(bm.group(3)))//2,
                    "y": (int(bm.group(2))+int(bm.group(4)))//2,
                })
    
    stations = [s for s in stations if "搜索框" not in s["name"]]
    print(f"  找到 {len(stations)} 个站点，取前2个")
    
    for si, s in enumerate(stations[:2]):
        if s["name"] in seen:
            print(f"    [{si+1}] 跳过重复: {s['name'][:40]}")
            continue
        seen.add(s["name"])
        
        print(f"    [{si+1}] {s['name'][:50]}")
        
        # Click detail
        d.click(s["x"], s["y"]); time.sleep(3)
        xml1 = d.dump_hierarchy()
        
        # Save first dump
        safe_name = re.sub(r'[^\w]', '_', s["name"][:20])
        with open(rf"C:\Users\26381\Desktop\adb-first\batch_detail_{safe_name}.xml", "w", encoding="utf-8") as f:
            f.write(xml1)
        
        page_info = classify_detail_page(xml1)
        r1 = parse_detail_xml(xml1)
        
        # Debug: check station_name extraction
        if not r1["station_name"]:
            # Fallback: use the search result name
            r1["station_name"] = s["name"]
            print(f"      [fallback name: {s['name'][:40]}]")
        
        print(f"      类型: {page_info['type']}")
        
        # Adaptive collection
        xml2, xml3 = xml1, xml1
        r2, r3 = {}, {}
        price_details = []
        
        # click_to_expand MUST happen BEFORE scrolling (coordinates shift!)
        if page_info["type"] == "click_to_expand":
            m = re.search(r'<node[^>]*text="/度"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"', xml1)
            if not m:
                m = re.search(r'<node[^>]*text="查看"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"', xml1)
            if m:
                cx = (int(m.group(1)) + int(m.group(3))) // 2
                cy = (int(m.group(2)) + int(m.group(4))) // 2
                d.click(cx, cy); time.sleep(3)
                xml_price = d.dump_hierarchy()
                price_details = parse_price_detail_page(xml_price)
                r_price = parse_detail_xml(xml_price)
                if r_price.get("operator") and not r1.get("operator"):
                    r1["operator"] = r_price["operator"]
                d.press("back"); time.sleep(2)
        
        # Scroll for remaining data
        if page_info["scrolls_needed"] >= 1:
            d.swipe(*SCROLL_SMALL, duration=0.3); time.sleep(2)
            xml2 = d.dump_hierarchy(); r2 = parse_detail_xml(xml2)
        if page_info["scrolls_needed"] >= 2:
            d.swipe(*SCROLL_SMALL, duration=0.3); time.sleep(2)
            xml3 = d.dump_hierarchy(); r3 = parse_detail_xml(xml3)
        
        # Merge
        if page_info["scrolls_needed"] == 0:
            result = r1
        elif page_info["scrolls_needed"] == 1:
            result = merge_results(r1, r2)
        else:
            result = merge_results(merge_results(r1, r2), r3)
        
        if price_details:
            result["fast_prices"] = price_details
        
        result["search_city"] = city
        result["search_query"] = query
        result["page_type"] = page_info["type"]
        result["collected_at"] = datetime.now().isoformat()
        
        # Geocode
        addr = result.get("address") or result["station_name"].split("(")[0]
        geo = geocode(addr, city)
        if geo:
            result["longitude"] = geo["longitude"]
            result["latitude"] = geo["latitude"]
        
        all_results.append(result)
        
        # Print safe summary
        nm = result["station_name"][:25] if result["station_name"] else "?"
        op = result.get("operator") or "-"
        pr = result.get("current_price") or "-"
        ln = result.get("longitude") or "?"
        lt = result.get("latitude") or "?"
        print(f"      => {nm} | op:{op} | price:{pr} | loc:({ln},{lt}) | type:{page_info['type']} | periods:{len(price_details)}")
        
        d.press("back"); time.sleep(2)

# Save
output = {"total": len(all_results), "stations": all_results}
with open(r"C:\Users\26381\Desktop\adb-first\batch_test_result.json", "w", encoding="utf-8") as f:
    json.dump(output, f, ensure_ascii=False, indent=2)

print(f"\nCompleted: {len(all_results)} stations saved to batch_test_result.json")
