# 快速启动采集脚本（支持限定范围 + 视觉自检 + 自动入库）
#
# 用法:
#   python run_crawl.py --districts 中原区           # 限定区县
#   python run_crawl.py --city 郑州                  # 整个城市
#   python run_crawl.py --visual                    # 启用百炼视觉自检
#   python run_crawl.py --import-db                 # 采集完成后导入 MySQL
import argparse
import json
import os
from datetime import datetime


def import_to_mysql(stations, dedupe=True):
    def _num(v):
        if v is None or v == "":
            return None
        return float(v)
    """把采集结果写入 MySQL heavy_truck_stations 表"""
    import pymysql

    cfg = {
        "host": os.getenv("DB_HOST", ""),
        "port": int(os.getenv("DB_PORT", "3306")),
        "user": os.getenv("DB_USER", ""),
        "password": os.getenv("DB_PASSWORD", ""),
        "database": os.getenv("DB_NAME", ""),
        "charset": os.getenv("DB_CHARSET", "utf8mb4"),
        "connect_timeout": int(os.getenv("DB_CONNECT_TIMEOUT", "5")),
    }
    if not all(cfg.get(k) for k in ("host", "user", "database")):
        print("[!] 未配置 DB_* 环境变量，跳过入库")
        return 0

    conn = pymysql.connect(**cfg)
    cur = conn.cursor()

    existing = set()
    if dedupe:
        cur.execute("SELECT station_name, longitude, latitude FROM heavy_truck_stations")
        for row in cur.fetchall():
            existing.add((row[0], _num(row[1]), _num(row[2])))

    inserted = 0
    skipped = 0
    for s in stations:
        key = (s.get("station_name", ""), _num(s.get("longitude")), _num(s.get("latitude")))
        if dedupe and key in existing:
            skipped += 1
            continue
        try:
            cur.execute(
                """INSERT INTO heavy_truck_stations
                   (station_name, operator, address, city, business_hours,
                    current_price, parking_fee, occupancy_fee,
                    longitude, latitude,
                    fast_available, fast_total, fast_power,
                    super_available, super_total, super_power,
                    slow_available, slow_total, slow_power,
                    fast_prices, slow_prices, facilities, tags,
                    favorite_count, collected_at)
                   VALUES (%s,%s,%s,%s,%s, %s,%s,%s, %s,%s, %s,%s,%s, %s,%s,%s, %s,%s,%s, %s,%s,%s,%s, %s,%s)""",
                (
                    s.get("station_name", ""),
                    s.get("operator", ""),
                    s.get("address", ""),
                    s.get("search_city", s.get("city", "")),
                    s.get("business_hours", ""),
                    s.get("current_price", ""),
                    s.get("parking_fee", ""),
                    s.get("occupancy_fee", ""),
                    s.get("longitude"),
                    s.get("latitude"),
                    s.get("fast_available", ""),
                    s.get("fast_total", ""),
                    s.get("fast_power", ""),
                    s.get("super_available", ""),
                    s.get("super_total", ""),
                    s.get("super_power", ""),
                    s.get("slow_available", ""),
                    s.get("slow_total", ""),
                    s.get("slow_power", ""),
                    json.dumps(s.get("fast_prices", []), ensure_ascii=False) if s.get("fast_prices") else None,
                    json.dumps(s.get("slow_prices", []), ensure_ascii=False) if s.get("slow_prices") else None,
                    json.dumps(s.get("facilities", []), ensure_ascii=False) if s.get("facilities") else None,
                    json.dumps(s.get("tags", []), ensure_ascii=False) if s.get("tags") else None,
                    s.get("favorite_count", ""),
                    s.get("collected_at", datetime.now().isoformat()),
                )
            )
            existing.add(key)
            inserted += 1
        except Exception as e:
            print(f"  [!] 入库失败 {s.get('station_name', '?')}: {e}")

    conn.commit()
    cur.close()
    conn.close()
    print(f"  入库完成: 新增 {inserted} 条, 跳过重复 {skipped} 条, 共 {len(stations)} 条")
    return inserted


def main():
    ap = argparse.ArgumentParser(description="充电站采集启动器")
    ap.add_argument("--city", default="郑州", help="城市名，需与 CITY_DISTRICTS 的 key 一致")
    ap.add_argument("--districts", default=None, help="逗号分隔的区县列表，如: 中原区,金水区；不传则遍历全市")
    ap.add_argument("--visual", action="store_true", help="启用百炼 qwen3-vl-flash 视觉自检")
    ap.add_argument("--import-db", action="store_true", help="采集完成后自动导入 MySQL")
    ap.add_argument("--output", default="collected_data.json", help="结果输出文件")
    args = ap.parse_args()

    from crawler import AmapCrawler

    crawler = AmapCrawler()

    if args.visual:
        from visual_check import integrate_with_crawler, QianwenVisionAdapter
        integrate_with_crawler(crawler, QianwenVisionAdapter())
        print("视觉自检已启用 (qwen3-vl-flash)")

    if args.districts:
        districts = [d.strip() for d in args.districts.split(",") if d.strip()]
        print(f"限定区县模式: {args.city} / {districts}")
        for district in districts:
            try:
                crawler.run_district(args.city, district)
            except Exception as e:
                print(f"  [!] {district} 采集异常: {e}")
    else:
        print(f"整市模式: {args.city}（按区县遍历）")
        crawler.run_city(args.city)

    crawler.deduplicate_results()
    crawler.save_results(args.output)
    print(f"\n完成，共 {len(crawler.results)} 条，已保存到 {args.output}")

    if args.import_db:
        for r in crawler.results:
            r.setdefault("city", args.city)
        import_to_mysql(crawler.results)


if __name__ == "__main__":
    main()

