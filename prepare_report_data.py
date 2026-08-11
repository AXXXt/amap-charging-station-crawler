import argparse
import json
import os
import re
import sqlite3
from collections import Counter
from pathlib import Path

import requests

from batch_runner import evaluate_detail
from crawler import AMAP_API_KEY, CITY_DISTRICTS
from task_queue import StationTaskQueue, utc_now_iso


def text(value):
    if value is None:
        return ""
    if isinstance(value, list):
        return ""
    return str(value).strip()


CITY_LABELS = {
    city: city if city.endswith("市") else f"{city}市"
    for city in CITY_DISTRICTS
}
DISTRICT_TO_CITY = {
    district: CITY_LABELS[city]
    for city, districts in CITY_DISTRICTS.items()
    for district in districts
    if district != city
}
DISTRICT_ALIASES = {}
for district, parent_city in DISTRICT_TO_CITY.items():
    DISTRICT_ALIASES[district] = (district, parent_city)
    short_name = re.sub(r"[区县市]$", "", district)
    if len(short_name) >= 2 and short_name not in CITY_LABELS:
        DISTRICT_ALIASES.setdefault(short_name, (district, parent_city))


def parse_region(*values):
    combined = " ".join(filter(None, (text(value) for value in values)))
    if not combined:
        return "", ""

    district_matches = []
    for alias, (district, parent_city) in DISTRICT_ALIASES.items():
        index = combined.find(alias)
        if index >= 0:
            district_matches.append((index, -len(alias), district, parent_city))

    district = ""
    district_city = ""
    if district_matches:
        _, _, district, district_city = min(district_matches)

    city_matches = []
    for city, city_label in CITY_LABELS.items():
        for candidate in (city_label, city):
            index = combined.find(candidate)
            if index >= 0:
                city_matches.append((index, -len(candidate), city_label))
                break

    city = min(city_matches)[2] if city_matches else ""
    if district_city:
        city = district_city
    return city, district


def equipment_summary(result):
    parts = []
    labels = {"super": "超充", "fast": "快充", "slow": "慢充"}
    for prefix, label in labels.items():
        available = text(result.get(f"{prefix}_available"))
        total = text(result.get(f"{prefix}_total"))
        power = text(result.get(f"{prefix}_power"))
        if available or total or power:
            count_text = f"{available}/{total}" if available or total else ""
            parts.append(" ".join(part for part in (label, count_text, power) if part))
    return "；".join(parts)


def price_schedule_text(result):
    prices = list(result.get("fast_prices") or []) + list(result.get("slow_prices") or [])
    parts = []
    for item in prices:
        time_range = text(item.get("time"))
        price = text(item.get("total_price") or item.get("price"))
        electricity = text(item.get("elec_fee"))
        service = text(item.get("service_fee"))
        detail = price
        if electricity or service:
            detail += f"（电{electricity or '-'}+服{service or '-'}）"
        parts.append(f"{time_range} {detail}".strip())
    return "；".join(parts)


class PoiMetadataCache:
    def __init__(self, cache_path, api_key=None):
        self.cache_path = Path(cache_path)
        self.api_key = (
            os.getenv("AMAP_API_KEY") or AMAP_API_KEY
            if api_key is None
            else api_key
        )
        if self.cache_path.exists():
            self.data = json.loads(self.cache_path.read_text(encoding="utf-8"))
        else:
            self.data = {}

    def get(self, station_id, longitude=None, latitude=None):
        metadata = dict(self.data.get(station_id) or {})
        if not metadata and self.api_key:
            try:
                response = requests.get(
                    "https://restapi.amap.com/v3/place/detail",
                    params={"key": self.api_key, "id": station_id, "output": "JSON"},
                    timeout=10,
                )
                payload = response.json()
                poi = (payload.get("pois") or [{}])[0] if payload.get("status") == "1" else {}
                metadata = {
                    "name": text(poi.get("name")),
                    "address": text(poi.get("address")),
                    "province": text(poi.get("pname")),
                    "city": text(poi.get("cityname")),
                    "district": text(poi.get("adname")),
                    "type": text(poi.get("type")),
                    "telephone": text(poi.get("tel")),
                    "location": text(poi.get("location")),
                    "updated_at": text(poi.get("timestamp")),
                }
            except Exception as error:
                metadata = {"error": str(error)}

        if (
            self.api_key
            and longitude not in (None, "")
            and latitude not in (None, "")
            and (not text(metadata.get("city")) or not text(metadata.get("district")))
        ):
            try:
                response = requests.get(
                    "https://restapi.amap.com/v3/geocode/regeo",
                    params={
                        "key": self.api_key,
                        "location": f"{longitude},{latitude}",
                        "extensions": "base",
                        "output": "JSON",
                    },
                    timeout=10,
                )
                payload = response.json()
                regeocode = payload.get("regeocode") or {}
                component = regeocode.get("addressComponent") or {}
                if payload.get("status") == "1":
                    metadata["address"] = text(metadata.get("address")) or text(
                        regeocode.get("formatted_address")
                    )
                    metadata["province"] = text(metadata.get("province")) or text(
                        component.get("province")
                    )
                    metadata["city"] = text(metadata.get("city")) or text(component.get("city"))
                    metadata["district"] = text(metadata.get("district")) or text(
                        component.get("district")
                    )
                    metadata["location"] = text(metadata.get("location")) or (
                        f"{longitude},{latitude}"
                    )
                    metadata["region_source"] = "reverse_geocode"
            except Exception as error:
                metadata.setdefault("reverse_geocode_error", str(error))

        self.data[station_id] = metadata
        return metadata

    def save(self):
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        self.cache_path.write_text(
            json.dumps(self.data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )


def build_detail_row(task, source, poi_metadata):
    result = task.get("result") or {}
    assessment = evaluate_detail(result)
    detail_address = text(result.get("address"))
    original_address = text(source.get("address"))
    effective_address = detail_address or text(poi_metadata.get("address")) or original_address
    parsed_city, parsed_district = parse_region(
        detail_address,
        original_address,
        result.get("station_name"),
        source.get("name"),
    )
    metadata_city, metadata_district = parse_region(
        poi_metadata.get("city"),
        poi_metadata.get("district"),
    )
    city = metadata_city or parsed_city
    district = metadata_district or parsed_district
    detailed = bool(task.get("detailed"))
    if detailed:
        coverage_status = "详细"
    elif result and result.get("detail_verified"):
        coverage_status = "部分"
    elif task.get("status") == "failed":
        coverage_status = "失败"
    else:
        coverage_status = task.get("status", "待处理")

    return {
        "sequence": text(source.get("sequence")),
        "station_id": task["station_id"],
        "original_name": text(source.get("name")),
        "detail_name": text(result.get("station_name")),
        "name_match": bool(result.get("name_match")),
        "original_address": original_address,
        "detail_address": detail_address,
        "effective_address": effective_address,
        "city": city,
        "district": district,
        "longitude": float(source["longitude"]) if text(source.get("longitude")) else None,
        "latitude": float(source["latitude"]) if text(source.get("latitude")) else None,
        "coverage_status": coverage_status,
        "score": float(task.get("completeness_score") or assessment["score"] or 0),
        "task_status": task.get("status", ""),
        "attempts": int(task.get("attempts") or 0),
        "match_method": text(result.get("match_method")),
        "business_hours": text(result.get("business_hours")),
        "operator": text(result.get("operator")),
        "current_price": text(result.get("current_price")),
        "parking_fee": text(result.get("parking_fee")),
        "occupancy_fee": text(result.get("occupancy_fee")),
        "facilities": "、".join(result.get("facilities") or []),
        "equipment": equipment_summary(result),
        "price_period_count": len(result.get("fast_prices") or []) + len(result.get("slow_prices") or []),
        "price_schedule": price_schedule_text(result),
        "collected_at_utc": text(result.get("collected_at")),
        "source_row": text(source.get("source_row")),
        "description": text(source.get("description")),
        "last_error": text(task.get("last_error")),
        "missing_fields": "、".join(assessment.get("missing") or []),
        "poi_api_name": text(poi_metadata.get("name")),
        "poi_api_type": text(poi_metadata.get("type")),
        "poi_api_updated_at": text(poi_metadata.get("updated_at")),
    }


def read_attempts(db_path):
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    rows = connection.execute(
        """
        SELECT station_id, attempt_no, device_serial, strategy, outcome,
               error, started_at, finished_at
        FROM task_attempts
        ORDER BY attempt_id
        """
    ).fetchall()
    connection.close()
    return [dict(row) for row in rows]


def prepare(db_path, output_path, cache_path, use_poi_api=True):
    queue = StationTaskQueue(db_path)
    tasks = list(queue.iter_task_rows())
    cache = PoiMetadataCache(cache_path, api_key="" if not use_poi_api else None)
    details = []
    unique_rows = []

    for task in tasks:
        payload = task["payload"]
        source_records = payload.get("source_records") or [payload]
        result = task.get("result") or {}
        result_city, result_district = parse_region(
            result.get("address"),
            payload.get("address"),
            result.get("station_name"),
            payload.get("name"),
        )
        needs_metadata = not result_city or not result_district or not result.get("address")
        poi_metadata = (
            cache.get(
                task["station_id"],
                longitude=payload.get("longitude"),
                latitude=payload.get("latitude"),
            )
            if needs_metadata and use_poi_api
            else {}
        )
        expanded = [build_detail_row(task, source, poi_metadata) for source in source_records]
        details.extend(expanded)
        unique_row = dict(expanded[0])
        unique_row["duplicate_count"] = len(source_records)
        unique_rows.append(unique_row)

    cache.save()
    failures = [row for row in unique_rows if row["coverage_status"] != "详细"]
    status_counts = Counter(row["coverage_status"] for row in details)
    city_counts = {}
    for city in sorted({row["city"] or "未识别" for row in details}):
        city_rows = [row for row in details if (row["city"] or "未识别") == city]
        detailed_count = sum(row["coverage_status"] == "详细" for row in city_rows)
        city_counts[city] = {
            "total": len(city_rows),
            "detailed": detailed_count,
            "rate": detailed_count / len(city_rows) if city_rows else 0,
        }

    payload = {
        "generated_at": utc_now_iso(),
        "source_row_count": len(details),
        "unique_task_count": len(unique_rows),
        "detailed_row_count": status_counts.get("详细", 0),
        "detailed_row_rate": status_counts.get("详细", 0) / len(details) if details else 0,
        "status_counts": dict(status_counts),
        "queue_stats": queue.stats(),
        "city_summary": city_counts,
        "details": details,
        "unique_tasks": unique_rows,
        "failures": failures,
        "attempts": read_attempts(db_path),
    }
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return payload


def main():
    parser = argparse.ArgumentParser(description="准备充电站 Excel 报表数据")
    parser.add_argument("--db", default="data/station_tasks.db")
    parser.add_argument("--output", default="data/report_data.json")
    parser.add_argument("--cache", default="data/amap_poi_cache.json")
    parser.add_argument("--no-poi-api", action="store_true")
    args = parser.parse_args()
    payload = prepare(
        args.db,
        args.output,
        args.cache,
        use_poi_api=not args.no_poi_api,
    )
    print(
        json.dumps(
            {
                "source_rows": payload["source_row_count"],
                "unique_tasks": payload["unique_task_count"],
                "detailed_rows": payload["detailed_row_count"],
                "detailed_rate": payload["detailed_row_rate"],
                "failures": len(payload["failures"]),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
