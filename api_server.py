"""
api_server.py — 重卡充电站数据API服务
功能：
  1. 提供RESTful API供其他平台使用
  2. 支持按城市/经纬度/运营商筛选
  3. 支持数据导出
  4. MySQL持久化存储
"""
from fastapi import FastAPI, Query, HTTPException, Header
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional, List
import pymysql
import json
import os
import sqlite3
import secrets
import uuid
import hashlib
import time
from datetime import datetime, timezone, timedelta
import urllib.parse
import urllib.request
from site_exploration_bridge import (
    SiteExplorationBridge,
    parse_mysql_database_url,
    station_source_key,
)

app = FastAPI(
    title="重卡充电站数据服务",
    description="河南省重卡充电站信息查询API",
    version="1.0.0"
)

app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


def _load_local_env(path):
    """Load ignored local .env values without adding a runtime dependency."""
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8-sig") as handle:
        for raw_line in handle:
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip("\"").strip("\'")
            if key:
                os.environ.setdefault(key, value)


_load_local_env(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

# ============================================================
# MySQL CONFIG
# ============================================================
DATABASE_URL_CONFIG = parse_mysql_database_url(os.getenv("EVCS_DATABASE_URL", ""))
DB_CONFIG = {
    "host": os.getenv("DB_HOST", ""),
    "port": int(os.getenv("DB_PORT", "3306")),
    "user": os.getenv("DB_USER", ""),
    "password": os.getenv("DB_PASSWORD", ""),
    "database": os.getenv("DB_NAME", ""),
    "charset": os.getenv("DB_CHARSET", "utf8mb4"),
    "connect_timeout": int(os.getenv("DB_CONNECT_TIMEOUT", "5")),
}
# DB_* is the local MySQL used by the local scan results.  EVCS_DATABASE_URL
# may override DB_CONFIG for the remote site-exploration bridge, but local
# results must keep going to the operator's own 3306 database.
LOCAL_DB_CONFIG = dict(DB_CONFIG)
if DATABASE_URL_CONFIG:
    DB_CONFIG.update(DATABASE_URL_CONFIG)
LOCAL_RESULT_SYNC_ENABLED = os.getenv(
    "LOCAL_RESULT_SYNC_ENABLED", "1"
).strip().lower() not in {"0", "false", "no", "off"}

SITE_EXPLORATION_TASKS_ENABLED = os.getenv(
    "SITE_EXPLORATION_TASKS_ENABLED", "0"
).strip().lower() not in {"0", "false", "no", "off"}
# The site-exploration flow has its own MySQL schema and bridge.  Do not send
# mobile-control UUID tasks/observations to the legacy scan_task/station_result
# tables unless that legacy integration is explicitly enabled.
LEGACY_MYSQL_SYNC_ENABLED = os.getenv(
    "LEGACY_MYSQL_SYNC_ENABLED", "0"
).strip().lower() not in {"0", "false", "no", "off"}
site_exploration_bridge = SiteExplorationBridge(
    DB_CONFIG,
    enabled=SITE_EXPLORATION_TASKS_ENABLED and bool(DATABASE_URL_CONFIG or all(
        DB_CONFIG.get(key) for key in ("host", "user", "database")
    )),
)

def get_db():
    if not all(DB_CONFIG.get(key) for key in ("host", "user", "database")):
        return None
    try:
        return pymysql.connect(**DB_CONFIG)
    except Exception:
        return None


# ============================================================
# MODELS
# ============================================================
class ChargingStation(BaseModel):
    station_name: str
    operator: Optional[str] = ""
    address: Optional[str] = ""
    city: Optional[str] = ""
    business_hours: Optional[str] = ""
    current_price: Optional[str] = ""
    parking_fee: Optional[str] = ""
    occupancy_fee: Optional[str] = ""
    longitude: Optional[float] = None
    latitude: Optional[float] = None
    fast_available: Optional[str] = ""
    fast_total: Optional[str] = ""
    fast_power: Optional[str] = ""
    super_available: Optional[str] = ""
    super_total: Optional[str] = ""
    super_power: Optional[str] = ""
    slow_available: Optional[str] = ""
    slow_total: Optional[str] = ""
    slow_power: Optional[str] = ""
    fast_prices: Optional[str] = ""   # JSON string
    slow_prices: Optional[str] = ""   # JSON string
    facilities: Optional[str] = ""    # JSON string
    tags: Optional[str] = ""          # JSON string
    favorite_count: Optional[str] = ""
    collected_at: Optional[str] = ""


class StationResponse(BaseModel):
    id: int
    station_name: str
    operator: str
    address: str
    city: str
    longitude: Optional[float]
    latitude: Optional[float]
    current_price: str
    fast_available: str
    fast_total: str
    fast_power: str
    fast_prices: Optional[list] = None
    collected_at: str


# ============================================================
# INIT DB TABLE
# ============================================================
def init_db():
    """创建数据表（如果不存在）"""
    conn = get_db()
    if conn is None:
        raise HTTPException(503, "数据库不可用，请先配置MySQL连接")
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS heavy_truck_stations (
            id INT AUTO_INCREMENT PRIMARY KEY,
            station_name VARCHAR(255) NOT NULL COMMENT '站点名称',
            operator VARCHAR(100) DEFAULT '' COMMENT '运营商',
            address VARCHAR(500) DEFAULT '' COMMENT '详细地址',
            city VARCHAR(50) DEFAULT '' COMMENT '城市',
            business_hours VARCHAR(100) DEFAULT '' COMMENT '营业时间',
            current_price VARCHAR(20) DEFAULT '' COMMENT '实时电价',
            parking_fee VARCHAR(200) DEFAULT '' COMMENT '停车费',
            occupancy_fee VARCHAR(300) DEFAULT '' COMMENT '占位费',
            longitude DECIMAL(10,6) DEFAULT NULL COMMENT '经度',
            latitude DECIMAL(10,6) DEFAULT NULL COMMENT '纬度',
            fast_available VARCHAR(10) DEFAULT '' COMMENT '快充可用数',
            fast_total VARCHAR(10) DEFAULT '' COMMENT '快充枪数',
            fast_power VARCHAR(50) DEFAULT '' COMMENT '快充功率',
            super_available VARCHAR(10) DEFAULT '' COMMENT '超充可用数',
            super_total VARCHAR(10) DEFAULT '' COMMENT '超充枪数',
            super_power VARCHAR(50) DEFAULT '' COMMENT '超充功率',
            slow_available VARCHAR(10) DEFAULT '' COMMENT '慢充可用数',
            slow_total VARCHAR(10) DEFAULT '' COMMENT '慢充枪数',
            slow_power VARCHAR(50) DEFAULT '' COMMENT '慢充功率',
            fast_prices JSON DEFAULT NULL COMMENT '24h快充价格趋势',
            slow_prices JSON DEFAULT NULL COMMENT '24h慢充价格趋势',
            facilities JSON DEFAULT NULL COMMENT '设施列表',
            tags JSON DEFAULT NULL COMMENT '标签列表',
            favorite_count VARCHAR(10) DEFAULT '' COMMENT '收藏数',
            collected_at DATETIME DEFAULT CURRENT_TIMESTAMP COMMENT '采集时间',
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
            INDEX idx_city (city),
            INDEX idx_operator (operator),
            INDEX idx_location (longitude, latitude)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='重卡充电站数据表'
    """)
    conn.commit()
    cur.close()
    conn.close()


# ============================================================
# API ENDPOINTS
# ============================================================
@app.on_event("startup")
async def startup():
    init_mobile_db()
    try:
        task_conn = mobile_conn()
        try:
            henan_rows = task_conn.execute(
                "SELECT * FROM scan_task WHERE type = ? ORDER BY source_sequence, created_at, id",
                (HENAN_POI_DETAIL_TASK,),
            ).fetchall()
        finally:
            task_conn.close()
        # Use one remote transaction instead of opening one MySQL connection per
        # task.  This keeps startup bounded even when hundreds of POIs exist.
        _sync_henan_task_rows(henan_rows)
        print(f"  Henan POI task table ready ({len(henan_rows)} tasks)")
    except Exception as e:
        print(f"  Henan POI task table unavailable: {e}")
    if site_exploration_bridge.enabled:
        try:
            site_exploration_bridge.ensure_result_table()
            site_exploration_bridge.sync_next_site(mobile_conn)
            print("  Site exploration task source ready")
        except Exception as e:
            print(f"  Site exploration task source unavailable: {e}")
    else:
        seed_default_mobile_tasks()
    threading.Thread(target=_lease_reaper_loop, daemon=True).start()
    try:
        init_db()
        print("  MySQL connected")
    except Exception as e:
        print(f"  MySQL unavailable: {e}")
        print("  Running in offline mode (task management + JSON only)")


@app.get("/")
async def root():
    return {
        "service": "重卡充电站数据服务",
        "version": "1.0.0",
        "dashboard": "/dashboard",
        "endpoints": {
            "数据查询": [
                "GET /api/stations — 充电站列表（分页+筛选）",
                "GET /api/stations/{id} — 单站点详情",
                "GET /api/stations/nearby?lng=&lat=&radius= — 附近站点",
                "GET /api/stats — 统计概览",
                "POST /api/stations/batch — 批量导入",
            ],
            "任务管理": [
                "POST /api/tasks/start?cities=郑州&use_visual=false — 启动采集",
                "POST /api/tasks/stop — 停止采集",
                "GET /api/tasks/status — 采集进度",
                "GET /api/tasks/results — 当前结果",
            ]
        }
    }


@app.get("/health")
async def health():
    return {"status": "ok"}



def safe_db_query(query_func, default=None):
    """安全执行数据库查询，MySQL不可用时返回默认值"""
    try:
        return query_func()
    except Exception as e:
        return default

@app.get("/api/stations")
async def list_stations(
    city: Optional[str] = Query(None, description="城市筛选"),
    operator: Optional[str] = Query(None, description="运营商筛选"),
    page: int = Query(1, ge=1, description="页码"),
    page_size: int = Query(20, ge=1, le=100, description="每页数量"),
):
    """查询充电站列表，支持分页和筛选"""
    conn = get_db()
    if conn is None:
        return {"total": 0, "page": page, "page_size": page_size, "total_pages": 0, "data": [], "offline": True}
    cur = conn.cursor(pymysql.cursors.DictCursor)
    
    where = []
    params = []
    if city:
        where.append("city = %s"); params.append(city)
    if operator:
        where.append("operator = %s"); params.append(operator)

    where_clause = " WHERE " + " AND ".join(where) if where else ""

    # 数据源：采集结果统一表 site_exploration_charging_station_result（只读）
    # 字段经别名映射对齐历史响应契约；JSON 字段从 result_payload 提取
    from_sql = "FROM site_exploration_charging_station_result r"
    station_select = """SELECT r.id,
       COALESCE(NULLIF(r.matched_station_name,''), r.requested_name) AS station_name,
       r.operator,
       COALESCE(NULLIF(r.collected_address,''), r.source_address) AS address,
       r.city,
       JSON_UNQUOTE(JSON_EXTRACT(r.result_payload,'$.currentPrice')) AS current_price,
       r.source_longitude AS longitude,
       r.source_latitude AS latitude,
       CAST(NULLIF(r.fast_available,'') AS UNSIGNED) AS fast_available,
       CAST(NULLIF(r.fast_total,'') AS UNSIGNED) AS fast_total,
       r.fast_power AS fast_power,
       CAST(NULLIF(r.super_available,'') AS UNSIGNED) AS super_available,
       CAST(NULLIF(r.super_total,'') AS UNSIGNED) AS super_total,
       r.super_power AS super_power,
       CAST(NULLIF(r.slow_available,'') AS UNSIGNED) AS slow_available,
       CAST(NULLIF(r.slow_total,'') AS UNSIGNED) AS slow_total,
       r.slow_power AS slow_power,
       CASE WHEN r.result_payload IS NULL OR NOT JSON_VALID(r.result_payload) THEN NULL
            ELSE JSON_UNQUOTE(JSON_EXTRACT(r.result_payload,'$.fastPrices')) END AS fast_prices,
       CASE WHEN r.result_payload IS NULL OR NOT JSON_VALID(r.result_payload) THEN NULL
            ELSE JSON_UNQUOTE(JSON_EXTRACT(r.result_payload,'$.slowPrices')) END AS slow_prices,
       JSON_UNQUOTE(JSON_EXTRACT(r.result_payload,'$.businessHours')) AS business_hours,
       JSON_UNQUOTE(JSON_EXTRACT(r.result_payload,'$.parkingFee')) AS parking_fee,
       JSON_UNQUOTE(JSON_EXTRACT(r.result_payload,'$.occupancyFee')) AS occupancy_fee,
       CASE WHEN r.result_payload IS NULL OR NOT JSON_VALID(r.result_payload) THEN NULL
            ELSE JSON_UNQUOTE(JSON_EXTRACT(r.result_payload,'$.favoriteCount')) END AS favorite_count,
       CASE WHEN r.result_payload IS NULL OR NOT JSON_VALID(r.result_payload) THEN NULL
            ELSE JSON_UNQUOTE(JSON_EXTRACT(r.result_payload,'$.facilities')) END AS facilities,
       CASE WHEN r.result_payload IS NULL OR NOT JSON_VALID(r.result_payload) THEN NULL
            ELSE JSON_UNQUOTE(JSON_EXTRACT(r.result_payload,'$.tags')) END AS tags,
       UNIX_TIMESTAMP(r.created_at) AS collected_at"""

    # Count
    cur.execute(f"SELECT COUNT(*) as total {from_sql}{where_clause}", params)
    total = cur.fetchone()["total"]

    # Query
    offset = (page - 1) * page_size
    cur.execute(
        f"""{station_select}
            {from_sql}{where_clause}
            ORDER BY r.id DESC LIMIT %s OFFSET %s""",
        params + [page_size, offset]
    )
    rows = cur.fetchall()
    
    # Parse JSON fields
    for row in rows:
        for field in ["fast_prices", "slow_prices", "facilities", "tags"]:
            if row.get(field) and isinstance(row[field], str):
                try:
                    row[field] = json.loads(row[field])
                except: pass
    
    cur.close(); conn.close()
    
    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "total_pages": (total + page_size - 1) // page_size,
        "data": rows
    }


@app.get("/api/stations/{station_id:int}")
async def get_station(station_id: int):
    """查询单个充电站完整详情"""
    conn = get_db()
    if conn is None:
        return {"data": None, "offline": True}
    cur = conn.cursor(pymysql.cursors.DictCursor)
    cur.execute(
        """SELECT r.id,
       COALESCE(NULLIF(r.matched_station_name,''), r.requested_name) AS station_name,
       r.operator,
       COALESCE(NULLIF(r.collected_address,''), r.source_address) AS address,
       r.city,
       JSON_UNQUOTE(JSON_EXTRACT(r.result_payload,'$.currentPrice')) AS current_price,
       r.source_longitude AS longitude,
       r.source_latitude AS latitude,
       CAST(NULLIF(r.fast_available,'') AS UNSIGNED) AS fast_available,
       CAST(NULLIF(r.fast_total,'') AS UNSIGNED) AS fast_total,
       r.fast_power AS fast_power,
       CAST(NULLIF(r.super_available,'') AS UNSIGNED) AS super_available,
       CAST(NULLIF(r.super_total,'') AS UNSIGNED) AS super_total,
       r.super_power AS super_power,
       CAST(NULLIF(r.slow_available,'') AS UNSIGNED) AS slow_available,
       CAST(NULLIF(r.slow_total,'') AS UNSIGNED) AS slow_total,
       r.slow_power AS slow_power,
       CASE WHEN r.result_payload IS NULL OR NOT JSON_VALID(r.result_payload) THEN NULL
            ELSE JSON_UNQUOTE(JSON_EXTRACT(r.result_payload,'$.fastPrices')) END AS fast_prices,
       CASE WHEN r.result_payload IS NULL OR NOT JSON_VALID(r.result_payload) THEN NULL
            ELSE JSON_UNQUOTE(JSON_EXTRACT(r.result_payload,'$.slowPrices')) END AS slow_prices,
       JSON_UNQUOTE(JSON_EXTRACT(r.result_payload,'$.businessHours')) AS business_hours,
       JSON_UNQUOTE(JSON_EXTRACT(r.result_payload,'$.parkingFee')) AS parking_fee,
       JSON_UNQUOTE(JSON_EXTRACT(r.result_payload,'$.occupancyFee')) AS occupancy_fee,
       CASE WHEN r.result_payload IS NULL OR NOT JSON_VALID(r.result_payload) THEN NULL
            ELSE JSON_UNQUOTE(JSON_EXTRACT(r.result_payload,'$.favoriteCount')) END AS favorite_count,
       CASE WHEN r.result_payload IS NULL OR NOT JSON_VALID(r.result_payload) THEN NULL
            ELSE JSON_UNQUOTE(JSON_EXTRACT(r.result_payload,'$.facilities')) END AS facilities,
       CASE WHEN r.result_payload IS NULL OR NOT JSON_VALID(r.result_payload) THEN NULL
            ELSE JSON_UNQUOTE(JSON_EXTRACT(r.result_payload,'$.tags')) END AS tags,
       UNIX_TIMESTAMP(r.created_at) AS collected_at
       FROM site_exploration_charging_station_result r WHERE r.id = %s""",
        [station_id]
    )
    row = cur.fetchone()
    cur.close(); conn.close()
    
    if not row:
        raise HTTPException(status_code=404, detail="充电站不存在")
    
    for field in ["fast_prices", "slow_prices", "facilities", "tags"]:
        if row.get(field) and isinstance(row[field], str):
            try: row[field] = json.loads(row[field])
            except: pass
    
    return {"data": row}


@app.get("/api/stations/nearby")
async def nearby_stations(
    lng: float = Query(..., description="经度"),
    lat: float = Query(..., description="纬度"),
    radius: float = Query(50.0, description="搜索半径(公里)"),
    limit: int = Query(20, ge=1, le=100),
):
    """根据经纬度查询附近充电站（简化版Haversine公式）"""
    conn = get_db()
    if conn is None:
        return {"data": [], "offline": True}
    cur = conn.cursor(pymysql.cursors.DictCursor)
    cur.execute(
        """SELECT r.id,
                  COALESCE(NULLIF(r.matched_station_name,''), r.requested_name) AS station_name,
                  r.operator,
                  COALESCE(NULLIF(r.collected_address,''), r.source_address) AS address,
                  r.city,
                  JSON_UNQUOTE(JSON_EXTRACT(r.result_payload,'$.currentPrice')) AS current_price,
                  r.source_longitude AS longitude,
                  r.source_latitude AS latitude,
                  CAST(NULLIF(r.fast_available,'') AS UNSIGNED) AS fast_available,
                  CAST(NULLIF(r.fast_total,'') AS UNSIGNED) AS fast_total,
                  r.fast_power AS fast_power,
                  CAST(NULLIF(r.super_available,'') AS UNSIGNED) AS super_available,
                  CAST(NULLIF(r.super_total,'') AS UNSIGNED) AS super_total,
                  r.super_power AS super_power,
                  CAST(NULLIF(r.slow_available,'') AS UNSIGNED) AS slow_available,
                  CAST(NULLIF(r.slow_total,'') AS UNSIGNED) AS slow_total,
                  r.slow_power AS slow_power
           FROM site_exploration_charging_station_result r
           WHERE r.source_longitude IS NOT NULL AND r.source_latitude IS NOT NULL
             AND r.source_longitude != 0 AND r.source_latitude != 0"""
    )
    rows = cur.fetchall()
    cur.close(); conn.close()
    
    # Filter by distance
    import math
    def haversine(lon1, lat1, lon2, lat2):
        R = 6371.0
        dlon = math.radians(lon2 - lon1)
        dlat = math.radians(lat2 - lat1)
        a = math.sin(dlat/2)**2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon/2)**2
        return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1-a))
    
    nearby = [
        r for r in rows
        if r["longitude"] and r["latitude"] and haversine(lng, lat, float(r["longitude"]), float(r["latitude"])) <= radius
    ]
    nearby.sort(key=lambda r: haversine(lng, lat, float(r["longitude"]), float(r["latitude"])))
    
    return {"data": nearby[:limit]}


@app.get("/api/stats")
async def get_stats():
    """统计概览：各城市站点数量、运营商分布等"""
    conn = get_db()
    if conn is None:
        return {
            "total_stations": 0,
            "by_city": [],
            "by_operator": [],
            "latest_collection": None,
            "offline": True,
        }
    cur = conn.cursor(pymysql.cursors.DictCursor)
    
    # Total
    cur.execute("SELECT COUNT(*) as total FROM site_exploration_charging_station_result")
    total = cur.fetchone()["total"]

    # By city
    cur.execute("SELECT city, COUNT(*) as count FROM site_exploration_charging_station_result GROUP BY city ORDER BY count DESC")
    by_city = cur.fetchall()

    # By operator
    cur.execute("SELECT operator, COUNT(*) as count FROM site_exploration_charging_station_result WHERE operator IS NOT NULL AND operator != '' GROUP BY operator ORDER BY count DESC")
    by_operator = cur.fetchall()

    # Latest collection time
    cur.execute("SELECT UNIX_TIMESTAMP(MAX(created_at)) as latest FROM site_exploration_charging_station_result")
    latest = cur.fetchone()["latest"]
    
    cur.close(); conn.close()
    
    return {
        "total_stations": total,
        "by_city": by_city,
        "by_operator": by_operator,
        "latest_collection": str(latest) if latest else None
    }


@app.post("/api/stations/batch")
async def batch_import(stations: List[dict]):
    """批量导入充电站数据"""
    conn = get_db()
    if conn is None:
        raise HTTPException(503, "数据库不可用，请先配置MySQL连接")
    cur = conn.cursor()
    
    inserted = 0
    for s in stations:
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
            inserted += 1
        except Exception as e:
            print(f"  Batch insert error for {s.get('station_name', '?')}: {e}")
    
    conn.commit()
    cur.close(); conn.close()
    
    return {"inserted": inserted, "total": len(stations)}


# ============================================================


# ============================================================
# TASK MANAGEMENT (后台任务调度)
# ============================================================
import threading
import sys
import os

from task_queue import StationTaskQueue

# 全局任务状态
stop_event = threading.Event()
QUEUE_DB_PATH = os.getenv(
    "STATION_TASK_DB",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "station_tasks.db"),
)

task_state = {
    "running": False,
    "current_city": "",
    "current_station": "",
    "progress": {"done": 0, "total": 0},
    "log": [],
    "results": [],
    "started_at": None,
    "error": None,
}


class UserStationTask(BaseModel):
    id: Optional[str] = ""
    name: str
    address: Optional[str] = ""
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    priority: int = 1000
    max_attempts: int = 3


def get_station_queue():
    return StationTaskQueue(QUEUE_DB_PATH)


@app.post("/api/queue/stations")
async def enqueue_station_task(request: UserStationTask):
    """提交用户回传站点；当前任务安全完成后按最高优先级调度。"""
    payload = request.model_dump() if hasattr(request, "model_dump") else request.dict()
    priority = payload.pop("priority")
    max_attempts = payload.pop("max_attempts")
    queue = get_station_queue()
    station_id = queue.enqueue_user_task(
        payload,
        priority=priority,
        max_attempts=max_attempts,
    )
    return {
        "status": "queued",
        "station_id": station_id,
        "priority": priority,
        "task": queue.get_task(station_id),
    }


@app.get("/api/queue/status")
async def station_queue_status():
    return get_station_queue().stats()


@app.get("/api/queue/stations/{station_id}")
async def station_queue_task(station_id: str):
    task = get_station_queue().get_task(station_id)
    if task is None:
        raise HTTPException(status_code=404, detail="站点任务不存在")
    return {"task": task}

def _log(msg):
    """添加日志"""
    from datetime import datetime
    task_state["log"].append(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")
    if len(task_state["log"]) > 200:
        task_state["log"] = task_state["log"][-100:]

def _run_crawl_task(cities, use_visual=False, districts=None):
    """后台执行采集任务"""
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from crawler import AmapCrawler, CITY_DISTRICTS
    
    if stop_event.is_set():
        task_state["running"] = False
        return

    task_state["error"] = None
    task_state["log"] = []
    task_state["results"] = []
    task_state["started_at"] = datetime.now(timezone.utc).isoformat()
    
    _log(f"任务启动: {len(cities)} 个城市")
    
    try:
        # 初始化
        checker = None
        if use_visual:
            from visual_check import create_qianwen_checker
            checker = create_qianwen_checker()
            _log("视觉自检已启用 (qwen3-vl-flash)")
        
        crawler = AmapCrawler(visual_checker=checker, stop_event=stop_event)

        # Install progress hooks once. Re-wrapping these methods inside each
        # city/district loop would make later wrappers call earlier wrappers
        # recursively.
        original_search = crawler.search_stations
        def search_with_progress(
            city,
            query=None,
            recenter_only=False,
            station_handler=None,
        ):
            if not task_state["running"]:
                raise InterruptedError("Task stopped by user")
            task_state["current_station"] = query or ""
            if station_handler is None:
                result = original_search(city, query, recenter_only)
            else:
                result = original_search(
                    city,
                    query,
                    recenter_only,
                    station_handler,
                )
            if not recenter_only and result:
                _log(f"  找到 {len(result)} 个站点")
            return result
        crawler.search_stations = search_with_progress

        original_collect = crawler.collect_detail
        def collect_with_progress(station, city):
            if not task_state["running"]:
                raise InterruptedError("Task stopped by user")
            task_state["current_station"] = station["name"][:30]
            task_state["progress"]["done"] += 1
            result = original_collect(station, city)
            if result:
                name = (result.get("station_name", "?") or "?")[:25]
                status = (result.get("business_status", "") or "")[:6]
                addr = (result.get("address", "") or "")[:20]
                operator = (result.get("operator", "") or "")[:10]
                price = (result.get("current_price", "") or "")[:8]
                eq_parts = []
                for eq_type in ["super", "fast", "slow"]:
                    avail = result.get(f"{eq_type}_available", "")
                    total = result.get(f"{eq_type}_total", "")
                    if avail or total:
                        eq_parts.append(f"{eq_type[:2]}:{avail}/{total}")
                eq_str = " ".join(eq_parts) if eq_parts else "-"
                _log(f"  {name:<25} | {status:<6} | {price:<8} | {operator:<10} | {eq_str}")
                if task_state["progress"]["done"] % 5 == 0:
                    detail_parts = [f"   地址: {addr}"]
                    if result.get("fast_prices"):
                        detail_parts.append(f"   分时电价: {len(result['fast_prices'])}组")
                    if result.get("latitude"):
                        detail_parts.append(f"   坐标: {result['latitude']},{result['longitude']}")
                    for detail in detail_parts:
                        _log(detail)
            return result
        crawler.collect_detail = collect_with_progress

        # === District-only mode ===
        if districts:
            _log(f"District mode: {len(districts)} districts")
            for di, (city, district) in enumerate(districts):
                if not task_state["running"]:
                    break
                task_state["current_city"] = f"{city}/{district}"
                _log(f"Start: {city}/{district}")
                _log("  " + "-" * 70)
                
                try:
                    n = crawler.run_district(city, district)
                    _log(f"  District {district} done: {n} stations")
                except InterruptedError:
                    _log("  Stopped by user")
                    break
                except Exception as e:
                    _log(f"  District {district} error: {str(e)[:80]}")
            
            crawler.deduplicate_results()
            task_state["results"] = crawler.results
            task_state["progress"]["total"] = len(crawler.results)
            _log(f"Done: {len(crawler.results)} stations (deduped)")
            
            outpath = os.path.join(os.path.dirname(os.path.abspath(__file__)), "task_result.json")
            __import__("json").dump(
                {"total": len(crawler.results), "stations": crawler.results},
                open(outpath, "w", encoding="utf-8"),
                ensure_ascii=False, indent=2
            )
            _log(f"Saved: task_result.json")
            return
        
        
        total_cities = len(cities)
        for ci, city in enumerate(cities):
            task_state["current_city"] = city
            _log(f"开始采集: {city}")
            _log("  " + "-" * 95)
            _log(f"  {'站点名称':<25} | {'营业':<6} | {'电价':<8} | {'运营商':<10} | {'设备(可用/总)'}")
            _log("  " + "-" * 95)
            
            if not task_state["running"]:
                break
            
            try:
                n = crawler.run_city(city)
                _log(f"  城市 {city} 完成: {n} 个站点")
                
            except Exception as e:
                _log(f"  城市 {city} 异常: {str(e)[:80]}")
        
        # 去重
        crawler.deduplicate_results()
        task_state["results"] = crawler.results
        task_state["progress"]["total"] = len(crawler.results)
        _log(f"任务完成: 共 {len(crawler.results)} 个站点（去重后）")
        
        # 自动保存
        outpath = os.path.join(os.path.dirname(os.path.abspath(__file__)), "task_result.json")
        __import__('json').dump(
            {"total": len(crawler.results), "stations": crawler.results},
            open(outpath, "w", encoding="utf-8"),
            ensure_ascii=False, indent=2
        )
        _log(f"结果已保存: task_result.json")
        
    except Exception as e:
        task_state["error"] = str(e)
        _log(f"任务异常: {e}")
    finally:
        task_state["running"] = False
        task_state["current_city"] = ""
        task_state["current_station"] = ""


@app.post("/api/tasks/start")
async def start_task(
    cities: str = Query("郑州", description="城市列表，逗号分隔"),
    use_visual: bool = Query(False, description="是否启用视觉自检"),

    districts: str = Query(None, description="Districts: city:district,..."),
):
    """启动采集任务"""
    if task_state["running"]:
        raise HTTPException(400, "已有任务在运行，请先停止")
    
    city_list = [c.strip() for c in cities.split(",") if c.strip()]
    
    stop_event.clear()  # Reset stop signal for new task
    task_state["running"] = True
    task_state["progress"] = {"done": 0, "total": 0}
    task_state["log"] = []
    task_state["results"] = []
    task_state["current_city"] = ""
    task_state["current_station"] = ""
    task_state["error"] = None
    
    
    # Parse districts if provided
    district_list = None
    if districts:
        district_list = []
        for d in districts.split(","):
            d = d.strip()
            if ":" in d:
                city, district = d.split(":", 1)
                district_list.append((city.strip(), district.strip()))

    thread = threading.Thread(target=_run_crawl_task, args=(city_list, use_visual, district_list), daemon=True)
    thread.start()
    
    return {"status": "started", "cities": city_list, "visual": use_visual}


@app.post("/api/tasks/stop")
async def stop_task():
    """停止当前任务 — 设置停止标志 + 多途径打断手机当前操作"""
    task_state["running"] = False
    stop_event.set()  # Signal crawler to stop
    
    # 途径1: uiautomator2 发送 back 键
    try:
        import uiautomator2 as u2
        d = u2.connect("RFCXA0W194D")
        d.press("back")
    except:
        pass
    
    # 途径2: 直接通过 adb shell 发送 back 键（可打断阻塞中的 uiautomator2 操作）
    try:
        import subprocess
        for _ in range(3):
            subprocess.run(
                [r"C:\Users\26381\AppData\Local\Android\Sdk\platform-tools\adb.exe",
                 "-s", "RFCXA0W194D", "shell", "input", "keyevent", "KEYCODE_BACK"],
                capture_output=True, timeout=3
            )
            import time
            time.sleep(0.3)
    except:
        pass
    
    return {"status": "stopped"}
@app.get("/api/tasks/status")
async def task_status():
    """获取任务状态"""
    return {
        "running": task_state["running"],
        "current_city": task_state["current_city"],
        "current_station": task_state["current_station"],
        "progress": task_state["progress"],
        "started_at": task_state["started_at"],
        "log": task_state["log"][-50:],  # 最近50条
        "error": task_state["error"],
    }


@app.get("/api/tasks/results")
async def task_results():
    """获取当前任务结果"""
    results = task_state["results"]
    return {"total": len(results), "stations": results}


@app.get("/dashboard")
async def dashboard():
    """Web控制台"""
    from fastapi.responses import HTMLResponse
    dashboard_html = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dashboard.html")
    if os.path.exists(dashboard_html):
        with open(dashboard_html, "r", encoding="utf-8") as f:
            return HTMLResponse(f.read())
    return HTMLResponse("<h1>dashboard.html not found</h1>")


# ============================================================
# MOBILE DEVICE CONTROL PLANE (SQLite fallback, no MySQL in APK)
# ============================================================
MOBILE_DB_PATH = os.getenv(
    "MOBILE_CONTROL_DB",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "mobile_control.db"),
)
MOBILE_ACTIVATION_CODE = os.getenv("MOBILE_ACTIVATION_CODE", "dev-activate")
MOBILE_LEASE_SECONDS = int(os.getenv("MOBILE_LEASE_SECONDS", "600"))
MOBILE_ADMIN_API_KEY = os.getenv("MOBILE_ADMIN_API_KEY", "dev-admin-key")
MOBILE_LEASE_RECLAIM_SECONDS = int(os.getenv("MOBILE_LEASE_RECLAIM_SECONDS", "30"))
MOBILE_CLAIM_MAX_RETRIES = int(os.getenv("MOBILE_CLAIM_MAX_RETRIES", "3"))
HENAN_POI_DETAIL_TASK = "HENAN_POI_DETAIL"
HENAN_FAILED_RETRY_LIMIT = max(
    0,
    int(os.getenv("HENAN_FAILED_RETRY_LIMIT", "2")),
)
AMAP_API_KEY = os.getenv("AMAP_API_KEY", "")
AMAP_POI_PAGE_SIZE = int(os.getenv("AMAP_POI_PAGE_SIZE", "25"))
AMAP_POI_IMPORT_QPS_DELAY_SECONDS = float(os.getenv("AMAP_POI_IMPORT_QPS_DELAY_SECONDS", "0.35"))
HENAN_CITY_IMPORT_ORDER = [
    ("郑州市", "410100"),
    ("洛阳市", "410300"),
    ("开封市", "410200"),
    ("平顶山市", "410400"),
    ("安阳市", "410500"),
    ("鹤壁市", "410600"),
    ("新乡市", "410700"),
    ("焦作市", "410800"),
    ("濮阳市", "410900"),
    ("许昌市", "411000"),
    ("漯河市", "411100"),
    ("三门峡市", "411200"),
    ("南阳市", "411300"),
    ("商丘市", "411400"),
    ("信阳市", "411500"),
    ("周口市", "411600"),
    ("驻马店市", "411700"),
    ("济源市", "419001"),
]
_henan_import_state_lock = threading.Lock()
_henan_import_thread = None
_henan_import_state = {
    "jobId": "",
    "status": "IDLE",
    "ready": False,
    "currentCity": "",
    "citiesCompleted": 0,
    "citiesTotal": len(HENAN_CITY_IMPORT_ORDER),
    "reportedTotal": 0,
    "fetched": 0,
    "created": 0,
    "skippedTask": 0,
    "skippedResult": 0,
    "skippedDuplicatePoi": 0,
    "failedCities": [],
    "message": "尚未开始导入",
    "startedAt": "",
    "updatedAt": "",
    "finishedAt": "",
}

# One physical station may be present in several source exploration rows.
# Keep only the earliest task for that station eligible for claiming.  The
# lease transaction below still provides the exact-task concurrency guarantee;
# this clause additionally prevents historical duplicate station tasks from
# being processed one after another.
SITE_TASK_CANONICAL_CLAUSE = """
                       AND (
                           COALESCE(scan_task.source_station_id, '') = ''
                           OR NOT EXISTS (
                               SELECT 1
                               FROM scan_task AS other_task
                               WHERE other_task.type = 'SITE_STATION_DETAIL'
                                 AND other_task.source_station_id = scan_task.source_station_id
                                 AND other_task.source_station_id <> ''
                                 AND other_task.id <> scan_task.id
                                 AND (
                                     other_task.status IN ('LEASED', 'RUNNING', 'COMPLETED')
                                     OR (
                                         other_task.status NOT IN ('FAILED', 'CANCELLED')
                                         AND (
                                             other_task.source_site_order < scan_task.source_site_order
                                             OR (
                                                 other_task.source_site_order = scan_task.source_site_order
                                                 AND other_task.source_sequence < scan_task.source_sequence
                                             )
                                             OR (
                                                 other_task.source_site_order = scan_task.source_site_order
                                                 AND other_task.source_sequence = scan_task.source_sequence
                                                 AND other_task.created_at < scan_task.created_at
                                             )
                                             OR (
                                                 other_task.source_site_order = scan_task.source_site_order
                                                 AND other_task.source_sequence = scan_task.source_sequence
                                                 AND other_task.created_at = scan_task.created_at
                                                 AND other_task.id < scan_task.id
                                             )
                                         )
                                     )
                                 )
                           )
                       )
"""


class MobileRegisterRequest(BaseModel):
    activationCode: str = ""
    deviceCode: str = ""
    name: str = ""
    appVersion: str = ""
    parserVersion: str = ""
    targetAppVersion: str = ""
    capabilities: dict = {}
    adbSerial: str = ""


class MobileHeartbeatRequest(BaseModel):
    status: str = "IDLE"
    lastError: str = ""
    appVersion: str = ""
    parserVersion: str = ""
    targetAppVersion: str = ""
    capabilities: dict = {}


class MobileClaimRequest(BaseModel):
    deviceCode: str = ""


class MobileTaskActionRequest(BaseModel):
    deviceCode: str = ""
    leaseToken: str = ""
    progress: Optional[dict] = None
    resultSummary: Optional[dict] = None
    errorCode: str = ""
    errorMessage: str = ""
    retryable: Optional[bool] = True


class MobileObservationUploadRequest(BaseModel):
    deviceCode: str = ""
    # Needed when uploading the currently active task. A stale worker cannot
    # upload after another device has reclaimed that lease.
    leaseToken: str = ""
    observations: List[dict] = []


class MobileTaskCreateItem(BaseModel):
    type: str = "REGION_SCAN"
    priority: int = 30
    province: str = ""
    city: str = ""
    district: str = ""
    keyword: str = ""
    searchRegion: str = ""
    maxAttempts: int = 3
    availableAt: str = ""


class HenanPoiImportRequest(BaseModel):
    keyword: str = "重卡充电站"
    province: str = "河南省"
    adcode: str = "410000"
    priority: int = 80
    maxAttempts: int = 3
    skipExistingResults: bool = True


class MobileTaskBatchCreateRequest(BaseModel):
    tasks: List[MobileTaskCreateItem] = []


def mobile_conn():
    directory = os.path.dirname(os.path.abspath(MOBILE_DB_PATH))
    os.makedirs(directory, exist_ok=True)
    conn = sqlite3.connect(MOBILE_DB_PATH, timeout=30.0)
    conn.row_factory = sqlite3.Row
    # WAL allows heartbeats/uploads to proceed around the short claim lock;
    # busy_timeout turns transient contention into a wait instead of a lost
    # polling cycle.
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_mobile_db():
    conn = mobile_conn()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS collector_device (
            id TEXT PRIMARY KEY,
            device_code TEXT NOT NULL UNIQUE,
            name TEXT NOT NULL,
            token_hash TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'IDLE',
            capabilities TEXT NOT NULL DEFAULT '{}',
            app_version TEXT NOT NULL DEFAULT '',
            parser_version TEXT NOT NULL DEFAULT '',
            target_app_version TEXT NOT NULL DEFAULT '',
            adb_serial TEXT NOT NULL DEFAULT '',
            last_heartbeat_at TEXT,
            last_error TEXT NOT NULL DEFAULT '',
            current_task_id TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS scan_task (
            id TEXT PRIMARY KEY,
            type TEXT NOT NULL,
            priority INTEGER NOT NULL DEFAULT 30,
            province TEXT NOT NULL DEFAULT '',
            city TEXT NOT NULL DEFAULT '',
            district TEXT NOT NULL DEFAULT '',
            keyword TEXT NOT NULL DEFAULT '',
            search_region TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL,
            assigned_device_id TEXT,
            lease_token TEXT,
            lease_expires_at TEXT,
            attempt INTEGER NOT NULL DEFAULT 0,
            max_attempts INTEGER NOT NULL DEFAULT 3,
            recovery_attempt INTEGER NOT NULL DEFAULT 0,
            max_recovery_attempts INTEGER NOT NULL DEFAULT 2,
            progress TEXT NOT NULL DEFAULT '{}',
            result_summary TEXT NOT NULL DEFAULT '{}',
            available_at TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL DEFAULT '',
            started_at TEXT,
            finished_at TEXT,
            last_error TEXT NOT NULL DEFAULT ''
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS station_observation (
            observation_id TEXT NOT NULL,
            device_id TEXT NOT NULL,
            task_id TEXT,
            station_id TEXT NOT NULL,
            captured_at TEXT NOT NULL,
            received_at TEXT NOT NULL,
            payload TEXT NOT NULL,
            PRIMARY KEY(device_id, observation_id)
        )
    """)
    device_columns = {
        row[1] for row in cur.execute("PRAGMA table_info(collector_device)").fetchall()
    }
    if "adb_serial" not in device_columns:
        cur.execute(
            "ALTER TABLE collector_device "
            "ADD COLUMN adb_serial TEXT NOT NULL DEFAULT ''"
        )
    task_columns = {row[1] for row in cur.execute("PRAGMA table_info(scan_task)").fetchall()}
    if "updated_at" not in task_columns:
        cur.execute("ALTER TABLE scan_task ADD COLUMN updated_at TEXT")
        cur.execute("UPDATE scan_task SET updated_at = created_at WHERE updated_at IS NULL")
    if "source_station_id" not in task_columns:
        cur.execute("ALTER TABLE scan_task ADD COLUMN source_station_id TEXT NOT NULL DEFAULT ''")
    if "source_payload" not in task_columns:
        cur.execute("ALTER TABLE scan_task ADD COLUMN source_payload TEXT NOT NULL DEFAULT '{}'")
    if "source_sequence" not in task_columns:
        cur.execute("ALTER TABLE scan_task ADD COLUMN source_sequence INTEGER NOT NULL DEFAULT 0")
    if "recovery_attempt" not in task_columns:
        cur.execute(
            "ALTER TABLE scan_task "
            "ADD COLUMN recovery_attempt INTEGER NOT NULL DEFAULT 0"
        )
    if "max_recovery_attempts" not in task_columns:
        cur.execute(
            "ALTER TABLE scan_task "
            f"ADD COLUMN max_recovery_attempts INTEGER NOT NULL DEFAULT {HENAN_FAILED_RETRY_LIMIT}"
        )
    # Tighten legacy queues without changing task state. Existing Henan rows
    # were created with three normal attempts / three recovery attempts.
    cur.execute(
        """UPDATE scan_task
           SET max_recovery_attempts = ?
           WHERE type = ? AND max_recovery_attempts > ?""",
        (HENAN_FAILED_RETRY_LIMIT, HENAN_POI_DETAIL_TASK, HENAN_FAILED_RETRY_LIMIT),
    )
    site_exploration_bridge.ensure_local_schema(conn)
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_scan_task_claim "
        "ON scan_task(status, available_at, priority DESC, created_at)"
    )
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_scan_task_station_claim "
        "ON scan_task(type, source_station_id, source_site_order, source_sequence, created_at)"
    )
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_scan_task_failed_recovery "
        "ON scan_task(type, status, recovery_attempt, available_at, source_sequence)"
    )
    cur.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_scan_task_device_active "
        "ON scan_task(assigned_device_id) "
        "WHERE status IN ('LEASED', 'RUNNING')"
    )
    cur.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_device_current_task "
        "ON collector_device(current_task_id) "
        "WHERE current_task_id IS NOT NULL AND current_task_id != ''"
    )
    cur.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_obs_task_station "
        "ON station_observation(task_id, station_id) "
        "WHERE task_id IS NOT NULL AND task_id != ''"
    )
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_obs_received "
        "ON station_observation(received_at)"
    )
    conn.commit()
    conn.close()


def seed_default_mobile_tasks():
    conn = mobile_conn()
    count = conn.execute("SELECT COUNT(*) AS c FROM scan_task").fetchone()["c"]
    if count > 0:
        conn.close()
        return
    regions = [
        ("郑州", "中原区"), ("郑州", "二七区"), ("郑州", "管城回族区"),
        ("郑州", "金水区"), ("郑州", "惠济区"), ("郑州", "中牟县"),
        ("洛阳", "涧西区"), ("洛阳", "洛龙区"), ("洛阳", "偃师区"),
        ("开封", "龙亭区"), ("南阳", "宛城区"), ("南阳", "卧龙区"),
        ("许昌", "魏都区"), ("平顶山", "新华区"), ("新乡", "红旗区"),
        ("安阳", "文峰区"), ("焦作", "解放区"), ("商丘", "梁园区"),
        ("周口", "川汇区"), ("驻马店", "驿城区"), ("信阳", "浉河区"),
        ("漯河", "源汇区"), ("三门峡", "湖滨区"), ("鹤壁", "淇滨区"),
        ("濮阳", "华龙区"), ("济源", "济源"),
    ]
    now = _mobile_utc()
    for city, district in regions:
        keyword = "重卡充电站" if district == city else f"{city}{district}重卡充电站"
        conn.execute(
            """INSERT INTO scan_task (
                   id, type, priority, province, city, district, keyword,
                   search_region, status, attempt, max_attempts, available_at, created_at, updated_at
               ) VALUES (?, 'REGION_SCAN', 30, '河南省', ?, ?, ?, '', 'PENDING', 0, 2, ?, ?, ?)""",
            (
                str(uuid.uuid4()),
                city,
                district,
                keyword,
                now,
                now,
                now,
            ),
        )
    conn.commit()
    conn.close()


def _mobile_utc():
    return datetime.now(timezone.utc).isoformat()


def _mobile_token_hash(token):
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _mobile_device_payload(row):
    return {
        "id": row["id"],
        "deviceCode": row["device_code"],
        "name": row["name"],
        "status": row["status"],
        "capabilities": json.loads(row["capabilities"] or "{}"),
        "appVersion": row["app_version"],
        "parserVersion": row["parser_version"],
        "targetAppVersion": row["target_app_version"],
        "lastHeartbeatAt": row["last_heartbeat_at"],
        "lastError": row["last_error"],
        "currentTaskId": row["current_task_id"],
        "createdAt": row["created_at"],
        "updatedAt": row["updated_at"],
    }


def _mobile_task_payload(row):
    if row is None:
        return None
    return {
        "id": row["id"],
        "type": row["type"],
        "priority": row["priority"],
        "province": row["province"],
        "city": row["city"],
        "district": row["district"],
        "keyword": row["keyword"],
        "searchRegion": row["search_region"],
        "status": row["status"],
        "assignedDeviceId": row["assigned_device_id"],
        "leaseToken": row["lease_token"],
        "leaseExpiresAt": row["lease_expires_at"],
        "attempt": row["attempt"],
        "maxAttempts": row["max_attempts"],
        "recoveryAttempt": int(
            row["recovery_attempt"] if "recovery_attempt" in row.keys() else 0
        ),
        "maxRecoveryAttempts": int(
            row["max_recovery_attempts"]
            if "max_recovery_attempts" in row.keys()
            else HENAN_FAILED_RETRY_LIMIT
        ),
        "progress": json.loads(row["progress"] or "{}"),
        "resultSummary": json.loads(row["result_summary"] or "{}"),
        "availableAt": row["available_at"],
        "createdAt": row["created_at"],
        "startedAt": row["started_at"],
        "finishedAt": row["finished_at"],
        "lastError": row["last_error"],
        **site_exploration_bridge.task_payload_fields(row),
        **_henan_poi_task_payload_fields(row),
    }


def _henan_poi_task_payload_fields(row):
    """Expose POI identity data for the mobile station-detail collector."""
    if row is None or row["type"] != HENAN_POI_DETAIL_TASK:
        return {}
    source_payload = _normalize_payload(row["source_payload"] if "source_payload" in row.keys() else {})
    source_payload = source_payload if isinstance(source_payload, dict) else {}
    return {
        "sourceStationId": str(row["source_station_id"] if "source_station_id" in row.keys() else ""),
        "stationId": str(row["source_station_id"] if "source_station_id" in row.keys() else ""),
        "stationName": str(source_payload.get("name") or row["keyword"] or ""),
        "stationAddress": str(source_payload.get("address") or ""),
        "stationLatitude": _optional_float(source_payload.get("latitude")),
        "stationLongitude": _optional_float(source_payload.get("longitude")),
        "stationSequence": int(row["source_sequence"] if "source_sequence" in row.keys() else 0),
        "sourcePayload": source_payload,
    }


def _require_mobile_device(authorization):
    token = ""
    if authorization and authorization.startswith("Bearer "):
        token = authorization[7:].strip()
    if not token:
        raise HTTPException(401, detail="DEVICE_TOKEN_REQUIRED")
    conn = mobile_conn()
    try:
        row = conn.execute(
            "SELECT * FROM collector_device WHERE token_hash = ?",
            (_mobile_token_hash(token),),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        raise HTTPException(401, detail="DEVICE_TOKEN_INVALID")
    return row


def _validate_device_code(device, requested_code):
    requested_code = (requested_code or "").strip()
    if requested_code and requested_code != device["device_code"]:
        raise HTTPException(403, detail="DEVICE_CODE_MISMATCH")


def _task_lease_is_active(task, device_id, lease_token, now=None):
    if task is None or task["status"] not in {"LEASED", "RUNNING"}:
        return False
    if task["assigned_device_id"] != device_id or task["lease_token"] != lease_token:
        return False
    expires_at = task["lease_expires_at"]
    if not expires_at:
        return False
    try:
        expires = datetime.fromisoformat(str(expires_at).replace("Z", "+00:00"))
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        return expires.astimezone(timezone.utc) >= (now or datetime.now(timezone.utc))
    except (TypeError, ValueError):
        return False


def _require_mobile_task(conn, device_id, task_id, lease_token):
    task = conn.execute(
        "SELECT * FROM scan_task WHERE id = ?",
        (task_id,),
    ).fetchone()

    def reject(status_code, detail):
        # Endpoint callers intentionally do not need a second finally block
        # just for validation failures; close this short-lived connection here.
        try:
            conn.rollback()
        finally:
            conn.close()
        raise HTTPException(status_code, detail=detail)

    if task is None:
        reject(404, "TASK_NOT_FOUND")
    if task["assigned_device_id"] != device_id or task["lease_token"] != lease_token:
        reject(409, "TASK_LEASE_STALE")
    if task["status"] not in {"LEASED", "RUNNING"}:
        reject(409, "TASK_NOT_ACTIVE")
    expires_at = task["lease_expires_at"]
    if not expires_at:
        reject(409, "TASK_LEASE_EXPIRED")
    try:
        expires = datetime.fromisoformat(str(expires_at).replace("Z", "+00:00"))
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        if expires.astimezone(timezone.utc) < datetime.now(timezone.utc):
            reject(409, "TASK_LEASE_EXPIRED")
    except (TypeError, ValueError):
        reject(409, "TASK_LEASE_INVALID")
    return task


def _require_admin_key(x_admin_key):
    expected = MOBILE_ADMIN_API_KEY
    if expected and (not x_admin_key or not secrets.compare_digest(x_admin_key, expected)):
        raise HTTPException(401, detail="ADMIN_API_KEY_REQUIRED")


def _mysql_configured():
    return all(DB_CONFIG.get(key) for key in ("host", "user", "database"))


def _normalize_payload(payload):
    if not isinstance(payload, str):
        return payload
    try:
        value = json.loads(payload)
        return value if isinstance(value, dict) else {"raw": payload}
    except (ValueError, TypeError):
        return {"raw": payload}

def _mysql_datetime(value):
    if not value:
        return None
    # Android outbox rows may use epoch milliseconds; normalize them before
    # inserting into MySQL DATETIME columns.
    try:
        epoch_value = int(value)
    except (TypeError, ValueError):
        epoch_value = None
    if epoch_value is not None:
        if epoch_value > 10_000_000_000:
            epoch_value /= 1000
        return datetime.fromtimestamp(
            epoch_value,
            timezone.utc,
        ).strftime("%Y-%m-%d %H:%M:%S")
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return str(value)[:19].replace("T", " ")


def _sync_mysql_task(task):
    if (
        task is None
        or not LEGACY_MYSQL_SYNC_ENABLED
        or not _mysql_configured()
    ):
        return
    try:
        conn = pymysql.connect(**DB_CONFIG)
        try:
            with conn.cursor() as cursor:
                cursor.execute(
                    """INSERT INTO scan_task (
                           id, type, priority, province, city, district, keyword,
                           search_region, status, assigned_device_id, lease_token,
                           lease_expires_at, attempt, max_attempts, progress,
                           result_summary, available_at, created_at, started_at,
                           finished_at, last_error
                       ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                                 %s, %s, %s, %s, %s, %s, %s, %s, %s)
                       ON DUPLICATE KEY UPDATE
                           type = VALUES(type), priority = VALUES(priority),
                           province = VALUES(province), city = VALUES(city),
                           district = VALUES(district), keyword = VALUES(keyword),
                           search_region = VALUES(search_region), status = VALUES(status),
                           assigned_device_id = VALUES(assigned_device_id),
                           lease_token = VALUES(lease_token),
                           lease_expires_at = VALUES(lease_expires_at),
                           attempt = VALUES(attempt), max_attempts = VALUES(max_attempts),
                           progress = VALUES(progress), result_summary = VALUES(result_summary),
                           available_at = VALUES(available_at), created_at = VALUES(created_at),
                           started_at = VALUES(started_at), finished_at = VALUES(finished_at),
                           last_error = VALUES(last_error)""",
                    (
                        task["id"], task["type"], task["priority"], task["province"],
                        task["city"], task["district"], task["keyword"], task["search_region"],
                        task["status"], task["assigned_device_id"], task["lease_token"],
                        _mysql_datetime(task["lease_expires_at"]), task["attempt"],
                        task["max_attempts"], task["progress"], task["result_summary"],
                        _mysql_datetime(task["available_at"]), _mysql_datetime(task["created_at"]),
                        _mysql_datetime(task["started_at"]), _mysql_datetime(task["finished_at"]),
                        task["last_error"],
                    ),
                )
            conn.commit()
        finally:
            conn.close()
    except Exception as error:
        print(f"MySQL task sync error: {error}", file=sys.stderr)


def _optional_float(value):
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _json_column(value):
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return value


def _local_db_configured():
    return all(LOCAL_DB_CONFIG.get(key) for key in ("host", "user", "database"))


def _local_result_exists(station_name: str) -> bool:
    """Whether the local table already contains a result for this station."""
    conn = pymysql.connect(**LOCAL_DB_CONFIG)
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                "SELECT 1 FROM heavy_truck_stations WHERE station_name = %s LIMIT 1",
                (station_name,),
            )
            return cursor.fetchone() is not None
    finally:
        conn.close()


def _sync_local_station_result(
    observation_id,
    station_id,
    device_id,
    payload,
    captured_at=None,
    received_at=None,
):
    """Upsert one local-scan result into the configured local MySQL table."""
    if (
        not LOCAL_RESULT_SYNC_ENABLED
        or not _local_db_configured()
        or not observation_id
        or not station_id
        or not isinstance(payload, dict)
    ):
        return
    station_name = str(payload.get("stationName") or "").strip()
    if not station_name:
        station_name = str(payload.get("requestedStationName") or "").strip()
    if not station_name:
        return
    # Remote SITE_STATION_DETAIL results belong to the remote site-exploration
    # table. Never overwrite the local scan table with those results.
    if str(payload.get("remoteTaskId") or "").strip():
        return
    if _local_result_exists(station_name):
        return

    values = {
        "station_name": station_name,
        "operator": str(payload.get("operator") or ""),
        "address": str(payload.get("address") or ""),
        "city": str(payload.get("city") or ""),
        "business_hours": str(payload.get("businessHours") or ""),
        "current_price": str(payload.get("currentPrice") or ""),
        "parking_fee": str(payload.get("parkingFee") or ""),
        "occupancy_fee": str(payload.get("occupancyFee") or ""),
        "longitude": _optional_float(payload.get("longitude")),
        "latitude": _optional_float(payload.get("latitude")),
        "fast_available": str(payload.get("fastAvailable") or ""),
        "fast_total": str(payload.get("fastTotal") or ""),
        "fast_power": str(payload.get("fastPower") or ""),
        "super_available": str(payload.get("superAvailable") or ""),
        "super_total": str(payload.get("superTotal") or ""),
        "super_power": str(payload.get("superPower") or ""),
        "slow_available": str(payload.get("slowAvailable") or ""),
        "slow_total": str(payload.get("slowTotal") or ""),
        "slow_power": str(payload.get("slowPower") or ""),
        "fast_prices": _json_column(payload.get("fastPrices")),
        "slow_prices": _json_column(payload.get("slowPrices")),
        "facilities": _json_column(payload.get("facilities")),
        "tags": _json_column(payload.get("tags")),
        "favorite_count": str(payload.get("favoriteCount") or ""),
        "collected_at": _mysql_datetime(captured_at or received_at),
    }

    conn = pymysql.connect(**LOCAL_DB_CONFIG)
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                "SELECT id FROM heavy_truck_stations "
                "WHERE station_name = %s ORDER BY id DESC LIMIT 1",
                (station_name,),
            )
            row = cursor.fetchone()
            if row is not None:
                assignments = ", ".join(
                    f"{column} = %s" for column in values
                )
                cursor.execute(
                    f"UPDATE heavy_truck_stations SET {assignments} "
                    "WHERE id = %s",
                    (*values.values(), row[0]),
                )
            else:
                columns = ", ".join(values)
                placeholders = ", ".join("%s" for _ in values)
                cursor.execute(
                    f"INSERT INTO heavy_truck_stations ({columns}) "
                    f"VALUES ({placeholders})",
                    tuple(values.values()),
                )
        conn.commit()
    finally:
        conn.close()


def _sync_mysql_observation(
    observation_id,
    station_id,
    task_id,
    device_id,
    payload,
    captured_at=None,
    received_at=None,
):
    if (
        not LEGACY_MYSQL_SYNC_ENABLED
        or not _mysql_configured()
        or not observation_id
        or not station_id
    ):
        return
    try:
        conn = pymysql.connect(**DB_CONFIG)
        try:
            with conn.cursor() as cursor:
                cursor.execute(
                    """INSERT INTO station_result (
                           idempotency_key, station_id, task_id, device_id,
                           result_json, uploaded, created_at, updated_at
                       ) VALUES (%s, %s, %s, %s, %s, 1, %s, %s)
                       ON DUPLICATE KEY UPDATE
                           station_id = VALUES(station_id), task_id = VALUES(task_id),
                           device_id = VALUES(device_id), result_json = VALUES(result_json),
                           uploaded = 1, updated_at = CURRENT_TIMESTAMP""",
                    (
                        observation_id,
                        station_id,
                        task_id,
                        device_id,
                        json.dumps(payload, ensure_ascii=False) if payload else None,
                        _mysql_datetime(captured_at or received_at),
                        _mysql_datetime(received_at),
                    ),
                )
            conn.commit()
        finally:
            conn.close()
    except Exception as error:
        print(f"MySQL observation sync error: {error}", file=sys.stderr)


def _sync_scan_task_by_id(task_id):
    if not _mysql_configured():
        return
    conn = mobile_conn()
    try:
        row = conn.execute("SELECT * FROM scan_task WHERE id = ?", (task_id,)).fetchone()
    finally:
        conn.close()
    _sync_mysql_task(row)


def _reap_expired_leases_once():
    now = _mobile_utc()
    conn = mobile_conn()
    reaped_ids = []
    try:
        conn.execute("BEGIN IMMEDIATE")
        stale = conn.execute(
            """SELECT id FROM scan_task
               WHERE status IN ('LEASED', 'RUNNING')
                 AND lease_expires_at IS NOT NULL
                 AND lease_expires_at < ?""",
            (now,),
        ).fetchall()
        for task in stale:
            row = conn.execute(
                """SELECT id, type, assigned_device_id, attempt, max_attempts,
                          recovery_attempt, max_recovery_attempts
                   FROM scan_task
                   WHERE id = ? AND status IN ('LEASED', 'RUNNING')
                     AND lease_expires_at IS NOT NULL
                     AND lease_expires_at < ?""",
                (task["id"], now),
            ).fetchone()
            if row is None:
                continue
            is_henan_recovery = (
                row["type"] == HENAN_POI_DETAIL_TASK
                and row["recovery_attempt"] > 0
            )
            will_retry_now = (
                not is_henan_recovery
                and row["attempt"] < row["max_attempts"]
            )
            next_status = "PENDING" if will_retry_now else "FAILED"
            conn.execute(
                """UPDATE scan_task
                   SET status = ?, assigned_device_id = NULL,
                       lease_token = NULL, lease_expires_at = NULL,
                       available_at = ?, updated_at = ?,
                       finished_at = CASE WHEN ? = 'FAILED' THEN ? ELSE NULL END,
                       last_error = CASE WHEN ? = 'FAILED' THEN 'LEASE_EXPIRED' ELSE '' END
                   WHERE id = ? AND status IN ('LEASED', 'RUNNING')""",
                (next_status, now, now, next_status, now, next_status, row["id"]),
            )
            reaped_ids.append(row["id"])
            if row["assigned_device_id"]:
                conn.execute(
                    """UPDATE collector_device
                       SET current_task_id = NULL, status = 'IDLE',
                           last_error = 'LEASE_EXPIRED', updated_at = ?
                       WHERE id = ? AND current_task_id = ?""",
                    (now, row["assigned_device_id"], row["id"]),
                )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    for task_id in reaped_ids:
        _sync_scan_task_by_id(task_id)
        conn = mobile_conn()
        try:
            row = conn.execute("SELECT * FROM scan_task WHERE id = ?", (task_id,)).fetchone()
        finally:
            conn.close()
        _sync_henan_task(row)

def _lease_reaper_loop():
    while True:
        try:
            _reap_expired_leases_once()
        except Exception as e:
            print(f"  Lease reaper error: {e}", file=sys.stderr)
        time.sleep(MOBILE_LEASE_RECLAIM_SECONDS)


@app.post("/api/v1/devices/register")
def mobile_register(request: MobileRegisterRequest):
    if not secrets.compare_digest(request.activationCode or "", MOBILE_ACTIVATION_CODE):
        raise HTTPException(403, detail="ACTIVATION_CODE_INVALID")
    device_code = (request.deviceCode or "").strip()
    if not device_code:
        raise HTTPException(400, detail="DEVICE_CODE_REQUIRED")
    now = _mobile_utc()
    token = secrets.token_urlsafe(32)
    conn = mobile_conn()
    existing = conn.execute(
        "SELECT id FROM collector_device WHERE device_code = ?",
        (device_code,),
    ).fetchone()
    device_id = existing["id"] if existing else str(uuid.uuid4())
    conn.execute(
        """INSERT INTO collector_device (
               id, device_code, name, token_hash, status, capabilities,
               app_version, parser_version, target_app_version,
               adb_serial, last_heartbeat_at, created_at, updated_at
           ) VALUES (?, ?, ?, ?, 'IDLE', ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(device_code) DO UPDATE SET
               name = excluded.name,
               token_hash = excluded.token_hash,
               capabilities = excluded.capabilities,
               app_version = excluded.app_version,
               parser_version = excluded.parser_version,
               target_app_version = excluded.target_app_version,
               adb_serial = COALESCE(NULLIF(excluded.adb_serial, ''), collector_device.adb_serial),
               status = 'IDLE',
               last_heartbeat_at = excluded.last_heartbeat_at,
               updated_at = excluded.updated_at""",
        (
            device_id,
            device_code,
            request.name or device_code,
            _mobile_token_hash(token),
            json.dumps(request.capabilities, ensure_ascii=False),
            request.appVersion,
            request.parserVersion,
            request.targetAppVersion,
            request.adbSerial,
            now,
            now,
            now,
        ),
    )
    row = conn.execute(
        "SELECT * FROM collector_device WHERE device_code = ?",
        (device_code,),
    ).fetchone()
    conn.commit()
    conn.close()
    return {
        "device": _mobile_device_payload(row),
        "deviceToken": token,
        "heartbeatIntervalSeconds": 15,
        "taskPollIntervalSeconds": 5,
    }


@app.post("/api/v1/devices/heartbeat")
def mobile_heartbeat(
    request: MobileHeartbeatRequest,
    authorization: Optional[str] = Header(None),
):
    device = _require_mobile_device(authorization)
    now = _mobile_utc()
    conn = mobile_conn()
    conn.execute(
        """UPDATE collector_device SET
               status = ?, capabilities = ?, app_version = ?, parser_version = ?,
               target_app_version = ?, last_error = ?, last_heartbeat_at = ?, updated_at = ?
           WHERE id = ?""",
        (
            request.status or device["status"],
            json.dumps(request.capabilities, ensure_ascii=False),
            request.appVersion,
            request.parserVersion,
            request.targetAppVersion,
            request.lastError[:500],
            now,
            now,
            device["id"],
        ),
    )
    row = conn.execute(
        "SELECT * FROM collector_device WHERE id = ?",
        (device["id"],),
    ).fetchone()
    conn.commit()
    conn.close()
    return {"device": _mobile_device_payload(row)}


@app.get("/api/v1/devices/me/config")
def mobile_config(authorization: Optional[str] = Header(None)):
    device = _require_mobile_device(authorization)
    return {
        "scanEnabled": True,
        "ruleVersion": "1.0.0",
        "pollIntervalSeconds": 5,
        "leaseSeconds": MOBILE_LEASE_SECONDS,
        "device": _mobile_device_payload(device),
    }


@app.post("/api/v1/device-tasks/claim")
def mobile_claim_task(
    request: MobileClaimRequest,
    authorization: Optional[str] = Header(None),
):
    device = _require_mobile_device(authorization)
    _validate_device_code(device, request.deviceCode)
    # Do not wait for the periodic reaper before taking over expired work.
    _reap_expired_leases_once()
    now = _mobile_utc()
    if site_exploration_bridge.enabled:
        try:
            site_exploration_bridge.sync_next_site(mobile_conn)
        except Exception as error:
            raise HTTPException(503, detail=f"SITE_TASK_SOURCE_UNAVAILABLE: {error}")
    conn = mobile_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        current = conn.execute(
            "SELECT * FROM collector_device WHERE id = ?",
            (device["id"],),
        ).fetchone()
        if current["current_task_id"]:
            task = conn.execute(
                "SELECT * FROM scan_task WHERE id = ?",
                (current["current_task_id"],),
            ).fetchone()
            if (
                task
                and task["status"] in {"LEASED", "RUNNING"}
                and task["lease_expires_at"]
                and task["lease_expires_at"] >= now
                and (
                    not site_exploration_bridge.enabled
                    or task["type"]
                    in {"SITE_STATION_DETAIL", HENAN_POI_DETAIL_TASK}
                )
            ):
                conn.commit()
                return {"task": _mobile_task_payload(task), "reason": "CURRENT_TASK"}
            if task:
                conn.execute(
                    """UPDATE collector_device
                       SET current_task_id = NULL, updated_at = ?
                       WHERE id = ? AND current_task_id = ?""",
                    (now, device["id"], current["current_task_id"]),
                )

        claimed = None
        claim_reason = "CLAIMED"
        for _ in range(MOBILE_CLAIM_MAX_RETRIES):
            canonical = site_exploration_bridge.enabled
            recovery_claim = False
            henan_pending = conn.execute(
                """SELECT 1 FROM scan_task
                   WHERE type = ? AND status = 'PENDING'
                     AND attempt < max_attempts LIMIT 1""",
                (HENAN_POI_DETAIL_TASK,),
            ).fetchone() is not None
            # Failed-task recovery starts only after all normal Henan work is done.
            # Active recovery leases do not block other phones from joining the wave.
            henan_primary_active = conn.execute(
                """SELECT 1 FROM scan_task
                   WHERE type = ? AND status IN ('LEASED', 'RUNNING')
                     AND recovery_attempt = 0 LIMIT 1""",
                (HENAN_POI_DETAIL_TASK,),
            ).fetchone() is not None
            henan_recovery_remaining = conn.execute(
                """SELECT 1 FROM scan_task
                   WHERE type = ? AND status = 'FAILED'
                     AND recovery_attempt < max_recovery_attempts LIMIT 1""",
                (HENAN_POI_DETAIL_TASK,),
            ).fetchone() is not None

            if henan_pending:
                task = conn.execute(
                    """SELECT * FROM scan_task
                       WHERE type = ? AND status = 'PENDING'
                         AND attempt < max_attempts AND available_at <= ?
                       ORDER BY source_sequence, created_at, id LIMIT 1""",
                    (HENAN_POI_DETAIL_TASK, now),
                ).fetchone()
            elif henan_primary_active:
                task = None
            elif henan_recovery_remaining:
                recovery_claim = True
                task = conn.execute(
                    """SELECT * FROM scan_task
                       WHERE type = ? AND status = 'FAILED'
                         AND recovery_attempt < max_recovery_attempts
                         AND available_at <= ?
                       ORDER BY recovery_attempt, source_sequence, created_at, id
                       LIMIT 1""",
                    (HENAN_POI_DETAIL_TASK, now),
                ).fetchone()
            elif canonical:
                task = conn.execute(
                    f"""SELECT * FROM scan_task
                       WHERE status = 'PENDING' AND attempt < max_attempts
                         AND available_at <= ? AND type = 'SITE_STATION_DETAIL'
                       {SITE_TASK_CANONICAL_CLAUSE}
                       ORDER BY source_site_order, source_sequence, created_at, id
                       LIMIT 1""",
                    (now,),
                ).fetchone()
            else:
                task = conn.execute(
                    f"""SELECT * FROM scan_task
                       WHERE status = 'PENDING' AND attempt < max_attempts
                         AND available_at <= ?
                       {SITE_TASK_CANONICAL_CLAUSE}
                       ORDER BY priority DESC, available_at, created_at, id
                       LIMIT 1""",
                    (now,),
                ).fetchone()
            if task is None:
                break
            lease_token = secrets.token_urlsafe(24)
            lease_expires_at = (
                datetime.now(timezone.utc) + timedelta(seconds=MOBILE_LEASE_SECONDS)
            ).isoformat()
            if recovery_claim:
                cur = conn.execute(
                    """UPDATE scan_task
                       SET status = 'LEASED', assigned_device_id = ?, lease_token = ?,
                           lease_expires_at = ?, recovery_attempt = recovery_attempt + 1,
                           finished_at = NULL, last_error = '', updated_at = ?
                       WHERE id = ? AND status = 'FAILED'
                         AND recovery_attempt < max_recovery_attempts""",
                    (device["id"], lease_token, lease_expires_at, now, task["id"]),
                )
            else:
                cur = conn.execute(
                    """UPDATE scan_task
                       SET status = 'LEASED', assigned_device_id = ?, lease_token = ?,
                           lease_expires_at = ?, attempt = attempt + 1,
                           started_at = COALESCE(started_at, ?),
                           last_error = '', updated_at = ?
                       WHERE id = ? AND status = 'PENDING'
                         AND attempt < max_attempts""",
                    (device["id"], lease_token, lease_expires_at, now, now, task["id"]),
                )
            if cur.rowcount != 1:
                continue
            conn.execute(
                """UPDATE collector_device
                   SET current_task_id = NULL, status = 'IDLE', updated_at = ?
                   WHERE current_task_id = ? AND id != ?""",
                (now, task["id"], device["id"]),
            )
            conn.execute(
                """UPDATE collector_device
                   SET current_task_id = ?, status = 'RUNNING', updated_at = ?
                   WHERE id = ?""",
                (task["id"], now, device["id"]),
            )
            claimed = conn.execute(
                "SELECT * FROM scan_task WHERE id = ?",
                (task["id"],),
            ).fetchone()
            if recovery_claim:
                claim_reason = "CLAIMED_FAILED_RETRY"
            break
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    if claimed is not None:
        _sync_mysql_task(claimed)
        _sync_henan_task(claimed)
    if claimed is None:
        return {"task": None, "reason": "QUEUE_EMPTY"}
    return {"task": _mobile_task_payload(claimed), "reason": claim_reason}


@app.post("/api/v1/device-tasks/{task_id}/ack")
def mobile_ack_task(
    task_id: str,
    request: MobileTaskActionRequest,
    authorization: Optional[str] = Header(None),
):
    device = _require_mobile_device(authorization)
    _validate_device_code(device, request.deviceCode)
    conn = mobile_conn()
    _require_mobile_task(conn, device["id"], task_id, request.leaseToken)
    cur = conn.execute(
        """UPDATE scan_task SET status = 'RUNNING', updated_at = ?
           WHERE id = ? AND assigned_device_id = ? AND lease_token = ?
             AND status IN ('LEASED', 'RUNNING')
             AND lease_expires_at >= ?""",
        (_mobile_utc(), task_id, device["id"], request.leaseToken, _mobile_utc()),
    )
    if cur.rowcount != 1:
        conn.rollback()
        conn.close()
        raise HTTPException(409, detail="TASK_LEASE_STALE")
    conn.commit()
    task = conn.execute("SELECT * FROM scan_task WHERE id = ?", (task_id,)).fetchone()
    conn.close()
    _sync_mysql_task(task)
    _sync_henan_task(task)
    return {"task": _mobile_task_payload(task)}


@app.post("/api/v1/device-tasks/{task_id}/progress")
def mobile_progress_task(
    task_id: str,
    request: MobileTaskActionRequest,
    authorization: Optional[str] = Header(None),
):
    device = _require_mobile_device(authorization)
    _validate_device_code(device, request.deviceCode)
    conn = mobile_conn()
    task = _require_mobile_task(conn, device["id"], task_id, request.leaseToken)
    merged = json.loads(task["progress"] or "{}")
    merged.update(request.progress or {})
    lease_expires_at = (
        datetime.now(timezone.utc) + timedelta(seconds=MOBILE_LEASE_SECONDS)
    ).isoformat()
    cur = conn.execute(
        """UPDATE scan_task SET status = 'RUNNING', progress = ?,
               lease_expires_at = ?, updated_at = ?
           WHERE id = ? AND assigned_device_id = ? AND lease_token = ?
             AND status IN ('LEASED', 'RUNNING')
             AND lease_expires_at >= ?""",
        (
            json.dumps(merged, ensure_ascii=False),
            lease_expires_at,
            _mobile_utc(),
            task_id,
            device["id"],
            request.leaseToken,
            _mobile_utc(),
        ),
    )
    if cur.rowcount != 1:
        conn.rollback()
        conn.close()
        raise HTTPException(409, detail="TASK_LEASE_STALE")
    conn.commit()
    task = conn.execute("SELECT * FROM scan_task WHERE id = ?", (task_id,)).fetchone()
    conn.close()
    _sync_mysql_task(task)
    _sync_henan_task(task)
    return {"task": _mobile_task_payload(task)}


@app.post("/api/v1/device-tasks/{task_id}/complete")
def mobile_complete_task(
    task_id: str,
    request: MobileTaskActionRequest,
    authorization: Optional[str] = Header(None),
):
    device = _require_mobile_device(authorization)
    _validate_device_code(device, request.deviceCode)
    now = _mobile_utc()
    conn = mobile_conn()
    _require_mobile_task(conn, device["id"], task_id, request.leaseToken)
    cur = conn.execute(
        """UPDATE scan_task SET status = 'COMPLETED', result_summary = ?,
               lease_token = NULL, lease_expires_at = NULL, finished_at = ?, updated_at = ?
           WHERE id = ? AND assigned_device_id = ? AND lease_token = ?
             AND status IN ('LEASED', 'RUNNING')
             AND lease_expires_at >= ?""",
        (
            json.dumps(request.resultSummary or {}, ensure_ascii=False),
            now,
            now,
            task_id,
            device["id"],
            request.leaseToken,
            now,
        ),
    )
    if cur.rowcount != 1:
        conn.rollback()
        conn.close()
        raise HTTPException(409, detail="TASK_LEASE_STALE")
    conn.execute(
        """UPDATE collector_device SET current_task_id = NULL, status = 'IDLE', updated_at = ?
           WHERE id = ? AND current_task_id = ?""",
        (now, device["id"], task_id),
    )
    conn.commit()
    task = conn.execute("SELECT * FROM scan_task WHERE id = ?", (task_id,)).fetchone()
    conn.close()
    _sync_mysql_task(task)
    _sync_henan_task(task)
    return {"task": _mobile_task_payload(task)}


@app.post("/api/v1/device-tasks/{task_id}/fail")
def mobile_fail_task(
    task_id: str,
    request: MobileTaskActionRequest,
    authorization: Optional[str] = Header(None),
):
    device = _require_mobile_device(authorization)
    _validate_device_code(device, request.deviceCode)
    now_value = datetime.now(timezone.utc)
    now = now_value.isoformat()
    conn = mobile_conn()
    task = _require_mobile_task(conn, device["id"], task_id, request.leaseToken)
    is_henan_recovery = (
        task["type"] == HENAN_POI_DETAIL_TASK
        and task["recovery_attempt"] > 0
    )
    # Normal attempts keep their existing immediate retry behavior. Once a
    # Henan task enters the recovery wave it returns to FAILED after each try,
    # so the dispatcher can finish the rest of that retry round first.
    should_retry_now = (
        not is_henan_recovery
        and bool(request.retryable)
        and task["attempt"] < task["max_attempts"]
    )
    has_deferred_retry = (
        task["type"] == HENAN_POI_DETAIL_TASK
        and task["recovery_attempt"] < task["max_recovery_attempts"]
    )
    next_status = "PENDING" if should_retry_now else "FAILED"
    needs_backoff = should_retry_now or has_deferred_retry
    retry_number = (
        task["recovery_attempt"] if is_henan_recovery else task["attempt"]
    )
    available_at = (
        now_value + timedelta(seconds=max(15, retry_number * 30))
    ).isoformat() if needs_backoff else now
    cur = conn.execute(
        """UPDATE scan_task SET status = ?, assigned_device_id = NULL,
               lease_token = NULL, lease_expires_at = NULL, last_error = ?,
               available_at = ?, updated_at = ?,
               finished_at = CASE WHEN ? = 'FAILED' THEN ? ELSE NULL END
           WHERE id = ? AND assigned_device_id = ? AND lease_token = ?
             AND status IN ('LEASED', 'RUNNING')
             AND lease_expires_at >= ?""",
        (
            next_status,
            f"{request.errorCode}:{request.errorMessage}"[:1000],
            available_at,
            now,
            next_status,
            now,
            task_id,
            device["id"],
            request.leaseToken,
            now,
        ),
    )
    if cur.rowcount != 1:
        conn.rollback()
        conn.close()
        raise HTTPException(409, detail="TASK_LEASE_STALE")
    device_status = "IDLE" if should_retry_now or has_deferred_retry else "FAULT"
    conn.execute(
        """UPDATE collector_device SET current_task_id = NULL,
               status = ?, last_error = ?, updated_at = ?
           WHERE id = ?""",
        (device_status, request.errorMessage[:500], now, device["id"]),
    )
    conn.commit()
    task = conn.execute("SELECT * FROM scan_task WHERE id = ?", (task_id,)).fetchone()
    conn.close()
    _sync_mysql_task(task)
    _sync_henan_task(task)
    return {
        "task": _mobile_task_payload(task),
        "requeued": should_retry_now,
        "deferredRetry": has_deferred_retry and not should_retry_now,
    }


# ============================================================
# ADB coordinate tap fallback for AMap overlay close buttons
# ============================================================
class MobileAdbTapRequest(BaseModel):
    deviceCode: str = ""
    x: int
    y: int
    reason: str = ""


def _resolve_adb_device_path() -> Optional[str]:
    candidates = [
        r"C:\Users\12495\.cache\codex-runtimes\android-build\sdk\platform-tools\adb.exe",
        r"C:\Users\12495\AppData\Local\Android\Sdk\platform-tools\adb.exe",
        "adb",
    ]
    for path in candidates:
        try:
            if path == "adb" or os.path.exists(path):
                return path
        except Exception:
            continue
    return None


@app.post("/api/v1/device/adb-tap")
def mobile_adb_tap(request: MobileAdbTapRequest, authorization: Optional[str] = Header(None)):
    device = _require_mobile_device(authorization)
    _validate_device_code(device, request.deviceCode)
    adb_path = _resolve_adb_device_path()
    if not adb_path:
        raise HTTPException(503, detail="ADB_NOT_AVAILABLE")
    serial = str(device.get("adb_serial") or "").strip() if isinstance(device, dict) else str(device["adb_serial"] or "").strip()
    if not serial:
        try:
            proc = subprocess.run(
                [adb_path, "devices"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            lines = [line.strip() for line in proc.stdout.splitlines() if "\tdevice" in line]
            serial = lines[0].split("\t")[0] if lines else ""
        except Exception:
            serial = ""
    if not serial:
        raise HTTPException(503, detail="ADB_DEVICE_NOT_FOUND")
    try:
        proc = subprocess.run(
            [adb_path, "-s", serial, "shell", "input", "tap", str(request.x), str(request.y)],
            capture_output=True,
            timeout=5,
        )
        success = proc.returncode == 0
    except Exception:
        success = False
    return {"success": success, "serial": serial, "reason": request.reason}


@app.post("/api/v1/observations/batches")
def mobile_upload_observations(
    request: MobileObservationUploadRequest,
    authorization: Optional[str] = Header(None),
):
    device = _require_mobile_device(authorization)
    _validate_device_code(device, request.deviceCode)
    now = _mobile_utc()
    now_value = datetime.now(timezone.utc)
    conn = mobile_conn()
    accepted = 0
    duplicates = 0
    accepted_ids = []
    duplicate_ids = []
    failed_ids = []
    errors = []
    mysql_observations = []
    task_cache = {}
    try:
        for item in request.observations:
            observation_id = str(item.get("observationId", ""))
            station_id = str(item.get("stationId", ""))
            task_id = str(item.get("taskId", "") or "")
            if not task_id:
                task_id = str(device["current_task_id"] or "")
            source_task = None
            if task_id:
                if task_id not in task_cache:
                    task_cache[task_id] = conn.execute(
                        "SELECT * FROM scan_task WHERE id = ?",
                        (task_id,),
                    ).fetchone()
                source_task = task_cache[task_id]
                # Active/requeued tasks require the current lease. Terminal
                # task retries remain idempotent so an outbox can drain after
                # a successful status update.
                if source_task is None:
                    failed_ids.append(observation_id)
                    errors.append(f"{observation_id}: TASK_NOT_FOUND")
                    continue
                if source_task["status"] not in {"COMPLETED", "FAILED", "CANCELLED"} and not _task_lease_is_active(
                    source_task, device["id"], request.leaseToken, now_value
                ):
                    failed_ids.append(observation_id)
                    errors.append(f"{observation_id}: TASK_LEASE_STALE")
                    continue
            payload = item.get("payload", {})
            payload = _normalize_payload(payload)
            if not observation_id or not station_id:
                if observation_id:
                    failed_ids.append(observation_id)
                errors.append("observationId and stationId are required")
                continue
            captured_at = str(item.get("capturedAt", now))
            existing = conn.execute(
                """SELECT * FROM station_observation
                   WHERE device_id = ? AND observation_id = ?""",
                (device["id"], observation_id),
            ).fetchone()
            existing_for_task_station = None
            if task_id:
                existing_for_task_station = conn.execute(
                    """SELECT * FROM station_observation
                       WHERE task_id = ? AND station_id = ?
                       LIMIT 1""",
                    (task_id, station_id),
                ).fetchone()

            payload_json = json.dumps(payload, ensure_ascii=False)
            if existing is not None:
                # Same device/idempotency key: refresh the durable local copy,
                # then let the remote upsert repair a previously interrupted
                # network delivery without creating a second row.
                conn.execute(
                    """UPDATE station_observation
                       SET task_id = ?, station_id = ?, captured_at = ?,
                           received_at = ?, payload = ?
                       WHERE device_id = ? AND observation_id = ?""",
                    (
                        task_id,
                        station_id,
                        captured_at,
                        now,
                        payload_json,
                        device["id"],
                        observation_id,
                    ),
                )
                duplicates += 1
                duplicate_ids.append(observation_id)
                sync_observation = (
                    observation_id,
                    station_id,
                    task_id,
                    payload,
                    captured_at,
                    source_task,
                    device["id"],
                )
            elif existing_for_task_station is not None:
                # A replacement device may submit the same task/station with a
                # different local key. Do not use INSERT OR REPLACE here: that
                # would delete the first device's row and make the result look
                # newly accepted. Keep the first durable observation and return
                # a duplicate acknowledgement for the replacement outbox.
                duplicates += 1
                duplicate_ids.append(observation_id)
                existing_payload = _normalize_payload(existing_for_task_station["payload"])
                existing_task = source_task
                if existing_for_task_station["task_id"] and existing_for_task_station["task_id"] != task_id:
                    existing_task = conn.execute(
                        "SELECT * FROM scan_task WHERE id = ?",
                        (existing_for_task_station["task_id"],),
                    ).fetchone()
                sync_observation = (
                    existing_for_task_station["observation_id"],
                    existing_for_task_station["station_id"],
                    existing_for_task_station["task_id"] or task_id,
                    existing_payload,
                    existing_for_task_station["captured_at"],
                    existing_task,
                    existing_for_task_station["device_id"],
                )
            else:
                conn.execute(
                    """INSERT INTO station_observation (
                           observation_id, device_id, task_id, station_id,
                           captured_at, received_at, payload
                       ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (
                        observation_id,
                        device["id"],
                        task_id,
                        station_id,
                        captured_at,
                        now,
                        payload_json,
                    ),
                )
                accepted += 1
                accepted_ids.append(observation_id)
                sync_observation = (
                    observation_id,
                    station_id,
                    task_id,
                    payload,
                    captured_at,
                    source_task,
                    device["id"],
                )

            mysql_observations.append(sync_observation)
        conn.commit()
    finally:
        conn.close()

    for observation in mysql_observations:
        _sync_mysql_observation(
            observation[0], observation[1], observation[2],
            observation[6], observation[3], observation[4], now,
        )
        if (
            observation[5] is None
            or observation[5]["type"] != HENAN_POI_DETAIL_TASK
        ) and LOCAL_RESULT_SYNC_ENABLED:
            try:
                _sync_local_station_result(
                    observation[0],
                    observation[1],
                    observation[6],
                    observation[3],
                    observation[4],
                    now,
                )
            except Exception as error:
                raise HTTPException(
                    503,
                    detail=f"LOCAL_RESULT_SYNC_UNAVAILABLE: {error}",
                )
        if site_exploration_bridge.enabled and observation[5] is not None:
            try:
                site_exploration_bridge.upsert_result(
                    task=observation[5],
                    observation_id=observation[0],
                    station_id=observation[1],
                    device_id=observation[6],
                    payload=observation[3],
                    captured_at=observation[4],
                    received_at=now,
                )
            except Exception as error:
                raise HTTPException(
                    503,
                    detail=f"SITE_RESULT_SYNC_UNAVAILABLE: {error}",
                )
    return {
        "accepted": accepted,
        "duplicates": duplicates,
        "acceptedIds": accepted_ids,
        "duplicateIds": duplicate_ids,
        "failedIds": failed_ids,
        "errors": errors,
    }


def _amap_get_json(path, params):
    query = urllib.parse.urlencode({"key": AMAP_API_KEY, **params})
    request = urllib.request.Request(
        f"https://restapi.amap.com{path}?{query}",
        headers={"User-Agent": "evcs-collector/1.0"},
    )
    with urllib.request.urlopen(request, timeout=12) as response:
        body = json.loads(response.read().decode("utf-8"))
    if str(body.get("status")) != "1":
        raise RuntimeError(f"AMAP_REQUEST_FAILED:{body.get('info', 'unknown')}")
    return body


def _amap_search_pois(keyword, adcode):
    """Fetch all pages exposed by Amap for one administrative area."""
    pois = []
    seen_page_ids = set()
    page = 1
    total = 0
    while True:
        body = _amap_get_json(
            "/v3/place/text",
            {
                "keywords": keyword,
                "city": adcode,
                "citylimit": "true",
                "extensions": "all",
                "offset": str(AMAP_POI_PAGE_SIZE),
                "page": str(page),
            },
        )
        page_pois = body.get("pois") or []
        try:
            total = int(body.get("count") or total or len(pois))
        except (TypeError, ValueError):
            total = len(pois)
        new_on_page = 0
        for poi in page_pois:
            poi_id = str(poi.get("id") or "").strip()
            dedupe_key = poi_id or json.dumps(poi, ensure_ascii=False, sort_keys=True)
            if dedupe_key in seen_page_ids:
                continue
            seen_page_ids.add(dedupe_key)
            pois.append(poi)
            new_on_page += 1
        if (
            not page_pois
            or new_on_page == 0
            or len(pois) >= total
            or page >= 200
        ):
            break
        page += 1
        time.sleep(AMAP_POI_IMPORT_QPS_DELAY_SECONDS)
    return pois, total, total > len(pois)


def _amap_district_children(adcode):
    """Return direct district/county children for a saturated city query."""
    body = _amap_get_json(
        "/v3/config/district",
        {
            "keywords": adcode,
            "subdistrict": "1",
            "extensions": "base",
        },
    )
    roots = body.get("districts") or []
    children = roots[0].get("districts") or [] if roots else []
    result = []
    seen = set()
    for item in children:
        child_code = str(item.get("adcode") or "").strip()
        child_name = str(item.get("name") or "").strip()
        if not child_code or child_code in seen:
            continue
        seen.add(child_code)
        result.append((child_name, child_code))
    return result


def _result_identity_sets():
    """Load unified results once so an import does not query MySQL per POI."""
    poi_ids = set()
    source_keys = set()
    conn = pymysql.connect(**DB_CONFIG)
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                f"SELECT source_station_id, requested_name, matched_station_name "
                f"FROM `{site_exploration_bridge.result_table}`"
            )
            for station_id, requested_name, matched_name in cursor.fetchall():
                station_id = str(station_id or "").strip()
                if station_id:
                    poi_ids.add(station_id)
                source_keys.add(
                    station_source_key(station_id, matched_name or requested_name or "")
                )
    finally:
        conn.close()
    return poi_ids, source_keys


def _henan_result_exists(poi_id, station_name=""):
    source_key = station_source_key(poi_id, station_name)
    conn = pymysql.connect(**DB_CONFIG)
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                f"SELECT 1 FROM `{site_exploration_bridge.result_table}` "
                "WHERE source_key = %s OR (%s <> '' AND source_station_id = %s) LIMIT 1",
                (source_key, str(poi_id or ""), str(poi_id or "")),
            )
            return cursor.fetchone() is not None
    finally:
        conn.close()


def _henan_import_snapshot():
    with _henan_import_state_lock:
        snapshot = dict(_henan_import_state)
        snapshot["failedCities"] = list(_henan_import_state.get("failedCities") or [])
        return snapshot


def _update_henan_import_state(**changes):
    with _henan_import_state_lock:
        _henan_import_state.update(changes)
        _henan_import_state["updatedAt"] = _mobile_utc()
        return dict(_henan_import_state)


def _ensure_henan_task_table():
    """Create the normalized remote task table for Henan Amap POI jobs."""
    conn = pymysql.connect(**DB_CONFIG)
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS henan_heavy_truck_charging_station_task (
                    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '主键',
                    task_key CHAR(64) COLLATE utf8mb4_general_ci NOT NULL COMMENT '任务稳定唯一键（高德POI编号SHA-256）',
                    local_task_id VARCHAR(64) COLLATE utf8mb4_general_ci NOT NULL DEFAULT '' COMMENT '本地调度表scan_task中的任务编号',
                    amap_poi_id VARCHAR(64) COLLATE utf8mb4_general_ci NOT NULL DEFAULT '' COMMENT '高德POI编号',
                    station_name VARCHAR(512) COLLATE utf8mb4_general_ci NOT NULL DEFAULT '' COMMENT '待采集充电站名称',
                    province VARCHAR(64) COLLATE utf8mb4_general_ci NOT NULL DEFAULT '' COMMENT '省份',
                    city VARCHAR(128) COLLATE utf8mb4_general_ci NOT NULL DEFAULT '' COMMENT '城市',
                    district VARCHAR(128) COLLATE utf8mb4_general_ci NOT NULL DEFAULT '' COMMENT '区县',
                    address VARCHAR(1000) COLLATE utf8mb4_general_ci NOT NULL DEFAULT '' COMMENT '高德POI地址',
                    longitude DECIMAL(12,8) NOT NULL DEFAULT 0.00000000 COMMENT '高德POI经度',
                    latitude DECIMAL(12,8) NOT NULL DEFAULT 0.00000000 COMMENT '高德POI纬度',
                    station_sequence INT UNSIGNED NOT NULL DEFAULT 0 COMMENT '导入时的任务顺序',
                    priority INT UNSIGNED NOT NULL DEFAULT 80 COMMENT '任务优先级，数值越大越优先',
                    status VARCHAR(32) COLLATE utf8mb4_general_ci NOT NULL DEFAULT 'PENDING' COMMENT '任务状态：PENDING/LEASED/RUNNING/COMPLETED/FAILED/CANCELLED',
                    attempt SMALLINT UNSIGNED NOT NULL DEFAULT 0 COMMENT '常规阶段已执行次数',
                    max_attempts SMALLINT UNSIGNED NOT NULL DEFAULT 3 COMMENT '常规阶段最大执行次数',
                    recovery_attempt SMALLINT UNSIGNED NOT NULL DEFAULT 0 COMMENT '常规任务结束后的失败任务补采次数',
                    max_recovery_attempts SMALLINT UNSIGNED NOT NULL DEFAULT 2 COMMENT '失败任务最大补采次数',
                    lease_device_id VARCHAR(64) COLLATE utf8mb4_general_ci NOT NULL DEFAULT '' COMMENT '当前租约设备编号',
                    lease_token VARCHAR(64) COLLATE utf8mb4_general_ci NOT NULL DEFAULT '' COMMENT '当前租约令牌',
                    lease_expires_at DATETIME NULL DEFAULT NULL COMMENT '租约到期时间',
                    available_at DATETIME NULL DEFAULT NULL COMMENT '最早可领取时间',
                    started_at DATETIME NULL DEFAULT NULL COMMENT '首次开始时间',
                    finished_at DATETIME NULL DEFAULT NULL COMMENT '终态时间',
                    last_error VARCHAR(1000) COLLATE utf8mb4_general_ci NOT NULL DEFAULT '' COMMENT '最近一次错误说明',
                    source_payload JSON NOT NULL COMMENT '导入来源原始数据',
                    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
                    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
                    PRIMARY KEY (id),
                    UNIQUE KEY uk_henan_station_task_key (task_key),
                    KEY idx_henan_station_task_amap_poi (amap_poi_id),
                    KEY idx_henan_station_task_claim (status, available_at, station_sequence),
                    KEY idx_henan_station_task_recovery (status, recovery_attempt, available_at, station_sequence),
                    KEY idx_henan_station_task_lease (lease_expires_at),
                    KEY idx_henan_station_task_created (created_at)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                  COLLATE=utf8mb4_general_ci
                  COMMENT='河南省重卡充电站采集任务表'
                """
            )
            cursor.execute("SHOW COLUMNS FROM henan_heavy_truck_charging_station_task")
            task_columns = {str(item[0]) for item in cursor.fetchall()}
            if "recovery_attempt" not in task_columns:
                cursor.execute(
                    "ALTER TABLE henan_heavy_truck_charging_station_task "
                    "ADD COLUMN recovery_attempt SMALLINT UNSIGNED NOT NULL DEFAULT 0 "
                    "COMMENT '常规任务结束后的失败任务补采次数' AFTER max_attempts"
                )
            if "max_recovery_attempts" not in task_columns:
                cursor.execute(
                    "ALTER TABLE henan_heavy_truck_charging_station_task "
                    f"ADD COLUMN max_recovery_attempts SMALLINT UNSIGNED NOT NULL "
                    f"DEFAULT {HENAN_FAILED_RETRY_LIMIT} COMMENT '失败任务最大补采次数' "
                    "AFTER recovery_attempt"
                )
            # Keep the remote mirror aligned with the local retry policy.
            cursor.execute(
                "UPDATE henan_heavy_truck_charging_station_task "
                "SET max_recovery_attempts = %s WHERE max_recovery_attempts > %s",
                (HENAN_FAILED_RETRY_LIMIT, HENAN_FAILED_RETRY_LIMIT),
            )
            cursor.execute("SHOW INDEX FROM henan_heavy_truck_charging_station_task")
            task_indexes = {str(item[2]) for item in cursor.fetchall()}
            if "idx_henan_station_task_recovery" not in task_indexes:
                cursor.execute(
                    "ALTER TABLE henan_heavy_truck_charging_station_task "
                    "ADD KEY idx_henan_station_task_recovery "
                    "(status, recovery_attempt, available_at, station_sequence)"
                )
        conn.commit()
    finally:
        conn.close()


def _henan_task_values(row):
    """Normalize one SQLite scan_task row for the remote Henan task table."""
    import json as _json

    payload = {}
    try:
        payload = _json.loads(row["source_payload"] or "{}")
    except (TypeError, ValueError):
        payload = {}
    payload = payload if isinstance(payload, dict) else {}
    station_name = str(payload.get("name") or row["keyword"] or "")
    task_key = hashlib.sha256(str(row["source_station_id"] or station_name).encode("utf-8")).hexdigest()
    longitude = _optional_float(payload.get("longitude"))
    latitude = _optional_float(payload.get("latitude"))
    return {
        "task_key": task_key,
        "local_task_id": str(row["id"] or ""),
        "amap_poi_id": str(row["source_station_id"] or payload.get("id") or ""),
        "station_name": station_name,
        "province": str(row["province"] or ""),
        "city": str(row["city"] or ""),
        "district": str(row["district"] or ""),
        "address": str(payload.get("address") or row["search_region"] or ""),
        "longitude": 0 if longitude is None else longitude,
        "latitude": 0 if latitude is None else latitude,
        "station_sequence": int(row["source_sequence"] if "source_sequence" in row.keys() else 0),
        "priority": int(row["priority"] or 80),
        "status": str(row["status"] or "PENDING"),
        "attempt": int(row["attempt"] or 0),
        "max_attempts": int(row["max_attempts"] or 3),
        "recovery_attempt": int(
            row["recovery_attempt"] if "recovery_attempt" in row.keys() else 0
        ),
        "max_recovery_attempts": int(
            row["max_recovery_attempts"]
            if "max_recovery_attempts" in row.keys()
            else HENAN_FAILED_RETRY_LIMIT
        ),
        "lease_device_id": str(row["assigned_device_id"] or ""),
        "lease_token": str(row["lease_token"] or ""),
        "lease_expires_at": _mysql_datetime(row["lease_expires_at"]),
        "available_at": _mysql_datetime(row["available_at"]),
        "started_at": _mysql_datetime(row["started_at"]),
        "finished_at": _mysql_datetime(row["finished_at"]),
        "last_error": str(row["last_error"] or "")[:1000],
        "source_payload": _json_column(payload) or "{}",
    }


def _sync_henan_task_rows(rows):
    """Mirror Henan POI task lifecycle rows using one remote transaction."""
    rows = [row for row in rows if row is not None and row["type"] == HENAN_POI_DETAIL_TASK]
    if not rows:
        return
    _ensure_henan_task_table()
    normalized = [_henan_task_values(row) for row in rows]
    columns = list(normalized[0])
    sql = """INSERT INTO henan_heavy_truck_charging_station_task
                 ({columns})
             VALUES ({placeholders})
             ON DUPLICATE KEY UPDATE {updates}""".format(
        columns=", ".join(columns),
        placeholders=", ".join("%s" for _ in columns),
        updates=", ".join(
            f"{column} = VALUES({column})"
            for column in columns
            if column not in {"task_key", "created_at"}
        ),
    )
    conn = pymysql.connect(**DB_CONFIG)
    try:
        with conn.cursor() as cursor:
            cursor.executemany(
                sql,
                [tuple(values[column] for column in columns) for values in normalized],
            )
        conn.commit()
    finally:
        conn.close()


def _sync_henan_task(row):
    _sync_henan_task_rows([row] if row is not None else [])


def _insert_henan_poi_tasks(
    pois,
    request_data,
    scope_name,
    scope_adcode,
    existing_task_ids,
    existing_result_ids,
    existing_result_keys,
    seen_job_ids,
    sequence_holder,
):
    counters = {
        "fetched": len(pois),
        "created": 0,
        "skippedTask": 0,
        "skippedResult": 0,
        "skippedDuplicatePoi": 0,
    }
    now = _mobile_utc()
    created_ids = []
    conn = mobile_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        for poi in pois:
            poi_id = str(poi.get("id") or "").strip()
            poi_name = str(poi.get("name") or "").strip()
            if not poi_id or not poi_name:
                counters["skippedDuplicatePoi"] += 1
                continue
            if poi_id in seen_job_ids:
                counters["skippedDuplicatePoi"] += 1
                continue
            seen_job_ids.add(poi_id)
            if poi_id in existing_task_ids:
                counters["skippedTask"] += 1
                continue
            result_key = station_source_key(poi_id, poi_name)
            if request_data.get("skipExistingResults", True) and (
                poi_id in existing_result_ids or result_key in existing_result_keys
            ):
                counters["skippedResult"] += 1
                continue
            location = str(poi.get("location") or "")
            location_parts = location.split(",", 1)
            longitude = _optional_float(location_parts[0] if location_parts else None)
            latitude = _optional_float(location_parts[1] if len(location_parts) > 1 else None)
            poi_payload = {
                "id": poi_id,
                "name": poi_name,
                "address": str(poi.get("address") or ""),
                "latitude": latitude,
                "longitude": longitude,
                "province": poi.get("pname") or request_data.get("province") or "河南省",
                "city": poi.get("cityname") or scope_name,
                "district": poi.get("adname") or "",
                "type": poi.get("type") or "",
                "importScope": scope_name,
                "importAdcode": scope_adcode,
            }
            sequence_holder[0] += 1
            task_id = str(uuid.uuid4())
            conn.execute(
                """INSERT INTO scan_task (
                       id, type, priority, province, city, district, keyword,
                       search_region, status, attempt, max_attempts,
                       available_at, created_at, updated_at,
                       source_station_id, source_sequence, source_payload
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'PENDING', 0, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    task_id,
                    HENAN_POI_DETAIL_TASK,
                    int(request_data.get("priority") or 80),
                    str(poi_payload.get("province") or "河南省"),
                    str(poi_payload.get("city") or scope_name),
                    str(poi_payload.get("district") or ""),
                    poi_name,
                    str(poi_payload.get("address") or ""),
                    int(request_data.get("maxAttempts") or 3),
                    now,
                    now,
                    now,
                    poi_id,
                    sequence_holder[0],
                    json.dumps(poi_payload, ensure_ascii=False),
                ),
            )
            created_ids.append(task_id)
            existing_task_ids.add(poi_id)
        conn.commit()
        if created_ids:
            placeholders = ",".join("?" for _ in created_ids)
            rows = conn.execute(
                f"SELECT * FROM scan_task WHERE id IN ({placeholders})",
                created_ids,
            ).fetchall()
        else:
            rows = []
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    _sync_henan_task_rows(rows)
    counters["created"] = len(created_ids)
    return counters


def _add_henan_import_counters(counters, reported_total=0):
    with _henan_import_state_lock:
        _henan_import_state["reportedTotal"] += int(reported_total or 0)
        for key in (
            "fetched", "created", "skippedTask",
            "skippedResult", "skippedDuplicatePoi",
        ):
            _henan_import_state[key] += int(counters.get(key) or 0)
        _henan_import_state["updatedAt"] = _mobile_utc()


def _run_henan_import_job(request_data, city_scopes):
    try:
        site_exploration_bridge.ensure_result_table()
        _ensure_henan_task_table()
        conn = mobile_conn()
        try:
            rows = conn.execute(
                "SELECT source_station_id FROM scan_task "
                "WHERE type = ? AND source_station_id <> ''",
                (HENAN_POI_DETAIL_TASK,),
            ).fetchall()
            existing_task_ids = {str(row[0]) for row in rows}
            max_sequence = conn.execute(
                "SELECT COALESCE(MAX(source_sequence), 0) FROM scan_task WHERE type = ?",
                (HENAN_POI_DETAIL_TASK,),
            ).fetchone()[0]
        finally:
            conn.close()
        existing_result_ids, existing_result_keys = _result_identity_sets()
        seen_job_ids = set()
        sequence_holder = [int(max_sequence or 0)]
        successful_cities = 0
        failed_cities = []
        keyword = str(request_data.get("keyword") or "重卡充电站").strip()

        for city_index, (city_name, city_adcode) in enumerate(city_scopes):
            _update_henan_import_state(
                currentCity=city_name,
                message=f"正在导入{city_name}候选站点",
            )
            try:
                pois, total, truncated = _amap_search_pois(keyword, city_adcode)
                counters = _insert_henan_poi_tasks(
                    pois, request_data, city_name, city_adcode,
                    existing_task_ids, existing_result_ids, existing_result_keys,
                    seen_job_ids, sequence_holder,
                )
                _add_henan_import_counters(counters, total)

                if truncated:
                    for district_name, district_adcode in _amap_district_children(city_adcode):
                        _update_henan_import_state(
                            currentCity=city_name,
                            message=f"{city_name}结果超限，正在补充{district_name}",
                        )
                        try:
                            district_pois, district_total, _ = _amap_search_pois(
                                keyword, district_adcode
                            )
                            district_counters = _insert_henan_poi_tasks(
                                district_pois, request_data, district_name, district_adcode,
                                existing_task_ids, existing_result_ids, existing_result_keys,
                                seen_job_ids, sequence_holder,
                            )
                            _add_henan_import_counters(district_counters, district_total)
                        except Exception as district_error:
                            failed_cities.append(
                                f"{city_name}/{district_name}: {district_error}"
                            )
                successful_cities += 1
                ready = _henan_import_snapshot().get("ready") or city_index == 0
                _update_henan_import_state(
                    ready=ready,
                    citiesCompleted=city_index + 1,
                    failedCities=failed_cities,
                    message=(
                        f"{city_name}导入完成，可开始采集；后台继续导入下一地市"
                        if city_index < len(city_scopes) - 1
                        else f"{city_name}导入完成"
                    ),
                )
            except Exception as city_error:
                failed_cities.append(f"{city_name}: {city_error}")
                _update_henan_import_state(
                    citiesCompleted=city_index + 1,
                    failedCities=failed_cities,
                    message=f"{city_name}导入失败，后台继续下一地市",
                )

        status = "COMPLETED" if not failed_cities else "COMPLETED_WITH_ERRORS"
        _update_henan_import_state(
            status=status,
            ready=successful_cities > 0,
            currentCity="",
            failedCities=failed_cities,
            message=(
                "河南省各地市候选站点导入完成"
                if not failed_cities
                else f"导入完成，{len(failed_cities)}个区域需要重试"
            ),
            finishedAt=_mobile_utc(),
        )
    except Exception as error:
        _update_henan_import_state(
            status="FAILED",
            currentCity="",
            message=f"河南POI后台导入失败: {error}",
            finishedAt=_mobile_utc(),
        )


@app.post("/api/v1/admin/henan-poi/import")
def mobile_admin_import_henan_pois(
    request: HenanPoiImportRequest,
    x_admin_key: Optional[str] = Header(None),
):
    """Start a city-by-city background import; Zhengzhou is always first."""
    global _henan_import_thread
    _require_admin_key(x_admin_key)
    if not AMAP_API_KEY:
        raise HTTPException(503, detail="AMAP_API_KEY_NOT_CONFIGURED")

    with _henan_import_state_lock:
        if _henan_import_thread is not None and _henan_import_thread.is_alive():
            snapshot = dict(_henan_import_state)
            snapshot["failedCities"] = list(_henan_import_state.get("failedCities") or [])
            snapshot["started"] = False
            return snapshot

        job_id = str(uuid.uuid4())
        started_at = _mobile_utc()
        _henan_import_state.update({
            "jobId": job_id,
            "status": "RUNNING",
            "ready": False,
            "currentCity": HENAN_CITY_IMPORT_ORDER[0][0],
            "citiesCompleted": 0,
            "citiesTotal": len(HENAN_CITY_IMPORT_ORDER),
            "reportedTotal": 0,
            "fetched": 0,
            "created": 0,
            "skippedTask": 0,
            "skippedResult": 0,
            "skippedDuplicatePoi": 0,
            "failedCities": [],
            "message": "后台导入已启动，正在导入郑州市",
            "startedAt": started_at,
            "updatedAt": started_at,
            "finishedAt": "",
        })
        request_data = request.dict()
        _henan_import_thread = threading.Thread(
            target=_run_henan_import_job,
            args=(request_data, list(HENAN_CITY_IMPORT_ORDER)),
            name=f"henan-poi-import-{job_id[:8]}",
            daemon=True,
        )
        _henan_import_thread.start()
        snapshot = dict(_henan_import_state)
    snapshot["started"] = True
    return snapshot


@app.get("/api/v1/admin/henan-poi/import-status")
def mobile_admin_henan_poi_import_status(
    x_admin_key: Optional[str] = Header(None),
):
    _require_admin_key(x_admin_key)
    return _henan_import_snapshot()


@app.get("/api/v1/admin/tasks")
def mobile_admin_tasks(
    status: Optional[str] = None,
    limit: int = Query(200, ge=1, le=1000),
    x_admin_key: Optional[str] = Header(None),
):
    _require_admin_key(x_admin_key)
    conn = mobile_conn()
    if status:
        rows = conn.execute(
            "SELECT * FROM scan_task WHERE status = ? ORDER BY created_at LIMIT ?",
            (status, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM scan_task ORDER BY created_at LIMIT ?",
            (limit,),
        ).fetchall()
    conn.close()
    return {"tasks": [_mobile_task_payload(row) for row in rows]}


@app.post("/api/v1/admin/tasks")
def mobile_admin_create_tasks(
    request: MobileTaskBatchCreateRequest,
    x_admin_key: Optional[str] = Header(None),
):
    _require_admin_key(x_admin_key)
    now = _mobile_utc()
    conn = mobile_conn()
    created_ids = []
    try:
        conn.execute("BEGIN IMMEDIATE")
        for item in request.tasks:
            task_id = str(uuid.uuid4())
            available_at = item.availableAt or now
            conn.execute(
                """INSERT INTO scan_task (
                       id, type, priority, province, city, district, keyword,
                       search_region, status, attempt, max_attempts,
                       available_at, created_at, updated_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'PENDING', 0, ?, ?, ?, ?)""",
                (
                    task_id,
                    item.type,
                    item.priority,
                    item.province,
                    item.city,
                    item.district,
                    item.keyword,
                    item.searchRegion,
                    item.maxAttempts,
                    available_at,
                    now,
                    now,
                ),
            )
            created_ids.append(task_id)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    if not created_ids:
        return {"created": 0, "tasks": []}
    conn = mobile_conn()
    placeholders = ",".join("?" for _ in created_ids)
    rows = conn.execute(
        f"SELECT * FROM scan_task WHERE id IN ({placeholders}) ORDER BY created_at",
        created_ids,
    ).fetchall()
    conn.close()
    for row in rows:
        _sync_mysql_task(row)
    return {
        "created": len(created_ids),
        "tasks": [_mobile_task_payload(row) for row in rows],
    }


@app.post("/api/v1/admin/site-tasks/rerun")
def mobile_admin_rerun_site_tasks(
    x_admin_key: Optional[str] = Header(None),
):
    """Reset every site task so collection restarts from the first source row.

    Existing observations and site_exploration_charging_station_result rows are
    intentionally preserved. Their stable source_key upsert will be overwritten
    as each station is collected again.
    """
    _require_admin_key(x_admin_key)
    now = _mobile_utc()
    conn = mobile_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        active = conn.execute(
            """SELECT id, keyword FROM scan_task
               WHERE type = ? AND status IN ('LEASED', 'RUNNING')
               LIMIT 1""",
            ('SITE_STATION_DETAIL',),
        ).fetchone()
        if active is not None:
            raise HTTPException(
                409,
                detail=f"SITE_TASK_ACTIVE: {active['keyword'] or active['id']}",
            )
        cur = conn.execute(
            """UPDATE scan_task
               SET status = 'PENDING', assigned_device_id = NULL,
                   lease_token = NULL, lease_expires_at = NULL,
                   attempt = 0, progress = '{}', result_summary = '{}',
                   available_at = ?, started_at = NULL, finished_at = NULL,
                   last_error = ''
               WHERE type = ?""",
            (now, 'SITE_STATION_DETAIL'),
        )
        conn.execute(
            """UPDATE collector_device
               SET current_task_id = NULL, status = 'IDLE', updated_at = ?
               WHERE current_task_id IN (
                   SELECT id FROM scan_task WHERE type = ?
               )""",
            (now, 'SITE_STATION_DETAIL'),
        )
        rows = conn.execute(
            """SELECT * FROM scan_task WHERE type = ?
               ORDER BY source_site_order, source_sequence""",
            ('SITE_STATION_DETAIL',),
        ).fetchall()
        conn.commit()
    except HTTPException:
        conn.rollback()
        raise
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    for row in rows:
        _sync_mysql_task(row)
    return {"reset": cur.rowcount, "firstTask": _mobile_task_payload(rows[0]) if rows else None}


@app.get("/api/v1/admin/observations")
def mobile_admin_observations(
    task_id: Optional[str] = None,
    device_id: Optional[str] = None,
    limit: int = Query(200, ge=1, le=1000),
    x_admin_key: Optional[str] = Header(None),
):
    _require_admin_key(x_admin_key)
    conn = mobile_conn()
    sql = "SELECT * FROM station_observation WHERE 1=1"
    params = []
    if task_id:
        sql += " AND task_id = ?"
        params.append(task_id)
    if device_id:
        sql += " AND device_id = ?"
        params.append(device_id)
    sql += " ORDER BY received_at DESC LIMIT ?"
    params.append(limit)
    rows = conn.execute(sql, params).fetchall()
    conn.close()
    observations = []
    for row in rows:
        observations.append({
            "observationId": row["observation_id"],
            "deviceId": row["device_id"],
            "taskId": row["task_id"],
            "stationId": row["station_id"],
            "capturedAt": row["captured_at"],
            "receivedAt": row["received_at"],
            "payload": json.loads(row["payload"] or "{}"),
        })
    return {"observations": observations, "count": len(observations)}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8800)
