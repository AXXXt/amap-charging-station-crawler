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
from datetime import datetime, timezone, timedelta

app = FastAPI(
    title="重卡充电站数据服务",
    description="河南省重卡充电站信息查询API",
    version="1.0.0"
)

app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# ============================================================
# MySQL CONFIG
# ============================================================
DB_CONFIG = {
    "host": os.getenv("DB_HOST", ""),
    "port": int(os.getenv("DB_PORT", "3306")),
    "user": os.getenv("DB_USER", ""),
    "password": os.getenv("DB_PASSWORD", ""),
    "database": os.getenv("DB_NAME", ""),
    "charset": os.getenv("DB_CHARSET", "utf8mb4"),
    "connect_timeout": int(os.getenv("DB_CONNECT_TIMEOUT", "5")),
}

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
    seed_default_mobile_tasks()
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
    
    # Count
    cur.execute(f"SELECT COUNT(*) as total FROM heavy_truck_stations{where_clause}", params)
    total = cur.fetchone()["total"]
    
    # Query
    offset = (page - 1) * page_size
    cur.execute(
        f"""SELECT id, station_name, operator, address, city, current_price,
                   longitude, latitude, fast_available, fast_total, fast_power,
                   super_available, super_total, super_power,
                   slow_available, slow_total, slow_power,
                   fast_prices, slow_prices, business_hours,
                   parking_fee, occupancy_fee, favorite_count,
                   facilities, tags, collected_at
            FROM heavy_truck_stations{where_clause}
            ORDER BY id DESC LIMIT %s OFFSET %s""",
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
        """SELECT * FROM heavy_truck_stations WHERE id = %s""",
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
        """SELECT id, station_name, operator, address, city, current_price,
                  longitude, latitude,
                  fast_available, fast_total, fast_power,
                  super_available, super_total, super_power,
                  slow_available, slow_total, slow_power
           FROM heavy_truck_stations
           WHERE longitude IS NOT NULL AND latitude IS NOT NULL"""
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
    cur.execute("SELECT COUNT(*) as total FROM heavy_truck_stations")
    total = cur.fetchone()["total"]
    
    # By city
    cur.execute("SELECT city, COUNT(*) as count FROM heavy_truck_stations GROUP BY city ORDER BY count DESC")
    by_city = cur.fetchall()
    
    # By operator
    cur.execute("SELECT operator, COUNT(*) as count FROM heavy_truck_stations WHERE operator != '' GROUP BY operator ORDER BY count DESC")
    by_operator = cur.fetchall()
    
    # Latest collection time
    cur.execute("SELECT MAX(collected_at) as latest FROM heavy_truck_stations")
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


class MobileRegisterRequest(BaseModel):
    activationCode: str = ""
    deviceCode: str = ""
    name: str = ""
    appVersion: str = ""
    parserVersion: str = ""
    targetAppVersion: str = ""
    capabilities: dict = {}


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
    observations: List[dict] = []


def mobile_conn():
    os.makedirs(os.path.dirname(MOBILE_DB_PATH), exist_ok=True)
    conn = sqlite3.connect(MOBILE_DB_PATH)
    conn.row_factory = sqlite3.Row
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
            progress TEXT NOT NULL DEFAULT '{}',
            result_summary TEXT NOT NULL DEFAULT '{}',
            available_at TEXT NOT NULL,
            created_at TEXT NOT NULL,
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
                   search_region, status, attempt, max_attempts, available_at, created_at
               ) VALUES (?, 'REGION_SCAN', 30, '河南省', ?, ?, ?, '', 'PENDING', 0, 3, ?, ?)""",
            (
                str(uuid.uuid4()),
                city,
                district,
                keyword,
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
        "progress": json.loads(row["progress"] or "{}"),
        "resultSummary": json.loads(row["result_summary"] or "{}"),
        "availableAt": row["available_at"],
        "createdAt": row["created_at"],
        "startedAt": row["started_at"],
        "finishedAt": row["finished_at"],
        "lastError": row["last_error"],
    }


def _require_mobile_device(authorization):
    token = ""
    if authorization and authorization.startswith("Bearer "):
        token = authorization[7:].strip()
    if not token:
        raise HTTPException(401, detail="DEVICE_TOKEN_REQUIRED")
    conn = mobile_conn()
    row = conn.execute(
        "SELECT * FROM collector_device WHERE token_hash = ?",
        (_mobile_token_hash(token),),
    ).fetchone()
    conn.close()
    if row is None:
        raise HTTPException(401, detail="DEVICE_TOKEN_INVALID")
    return row


def _require_mobile_task(conn, device_id, task_id, lease_token):
    task = conn.execute(
        "SELECT * FROM scan_task WHERE id = ?",
        (task_id,),
    ).fetchone()
    if task is None:
        raise HTTPException(404, detail="TASK_NOT_FOUND")
    if task["assigned_device_id"] != device_id or task["lease_token"] != lease_token:
        raise HTTPException(409, detail="TASK_LEASE_STALE")
    return task


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
               last_heartbeat_at, created_at, updated_at
           ) VALUES (?, ?, ?, ?, 'IDLE', ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(device_code) DO UPDATE SET
               name = excluded.name,
               token_hash = excluded.token_hash,
               capabilities = excluded.capabilities,
               app_version = excluded.app_version,
               parser_version = excluded.parser_version,
               target_app_version = excluded.target_app_version,
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
    now = _mobile_utc()
    conn = mobile_conn()
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
        ):
            conn.close()
            return {"task": _mobile_task_payload(task), "reason": "CURRENT_TASK"}
        if task:
            conn.execute(
                "UPDATE collector_device SET current_task_id = NULL, updated_at = ? WHERE id = ?",
                (now, device["id"]),
            )
    task = conn.execute(
        """SELECT * FROM scan_task
           WHERE status = 'PENDING' AND available_at <= ?
           ORDER BY priority DESC, available_at, created_at
           LIMIT 1""",
        (now,),
    ).fetchone()
    if task is None:
        conn.close()
        return {"task": None, "reason": "QUEUE_EMPTY"}
    lease_token = secrets.token_urlsafe(24)
    lease_expires_at = (
        datetime.now(timezone.utc) + timedelta(seconds=MOBILE_LEASE_SECONDS)
    ).isoformat()
    conn.execute(
        """UPDATE scan_task
           SET status = 'LEASED', assigned_device_id = ?, lease_token = ?,
               lease_expires_at = ?, attempt = attempt + 1,
               started_at = COALESCE(started_at, ?), last_error = ''
           WHERE id = ? AND status = 'PENDING'""",
        (device["id"], lease_token, lease_expires_at, now, task["id"]),
    )
    conn.execute(
        """UPDATE collector_device SET current_task_id = ?, status = 'RUNNING', updated_at = ?
           WHERE id = ?""",
        (task["id"], now, device["id"]),
    )
    conn.commit()
    claimed = conn.execute(
        "SELECT * FROM scan_task WHERE id = ?",
        (task["id"],),
    ).fetchone()
    conn.close()
    return {"task": _mobile_task_payload(claimed), "reason": "CLAIMED"}


@app.post("/api/v1/device-tasks/{task_id}/ack")
def mobile_ack_task(
    task_id: str,
    request: MobileTaskActionRequest,
    authorization: Optional[str] = Header(None),
):
    device = _require_mobile_device(authorization)
    conn = mobile_conn()
    _require_mobile_task(conn, device["id"], task_id, request.leaseToken)
    conn.execute(
        "UPDATE scan_task SET status = 'RUNNING' WHERE id = ?",
        (task_id,),
    )
    conn.commit()
    task = conn.execute("SELECT * FROM scan_task WHERE id = ?", (task_id,)).fetchone()
    conn.close()
    return {"task": _mobile_task_payload(task)}


@app.post("/api/v1/device-tasks/{task_id}/progress")
def mobile_progress_task(
    task_id: str,
    request: MobileTaskActionRequest,
    authorization: Optional[str] = Header(None),
):
    device = _require_mobile_device(authorization)
    conn = mobile_conn()
    task = _require_mobile_task(conn, device["id"], task_id, request.leaseToken)
    merged = json.loads(task["progress"] or "{}")
    merged.update(request.progress or {})
    lease_expires_at = (
        datetime.now(timezone.utc) + timedelta(seconds=MOBILE_LEASE_SECONDS)
    ).isoformat()
    conn.execute(
        """UPDATE scan_task SET status = 'RUNNING', progress = ?,
               lease_expires_at = ?
           WHERE id = ?""",
        (json.dumps(merged, ensure_ascii=False), lease_expires_at, task_id),
    )
    conn.commit()
    task = conn.execute("SELECT * FROM scan_task WHERE id = ?", (task_id,)).fetchone()
    conn.close()
    return {"task": _mobile_task_payload(task)}


@app.post("/api/v1/device-tasks/{task_id}/complete")
def mobile_complete_task(
    task_id: str,
    request: MobileTaskActionRequest,
    authorization: Optional[str] = Header(None),
):
    device = _require_mobile_device(authorization)
    now = _mobile_utc()
    conn = mobile_conn()
    _require_mobile_task(conn, device["id"], task_id, request.leaseToken)
    conn.execute(
        """UPDATE scan_task SET status = 'COMPLETED', result_summary = ?,
               lease_token = NULL, lease_expires_at = NULL, finished_at = ?
           WHERE id = ?""",
        (json.dumps(request.resultSummary or {}, ensure_ascii=False), now, task_id),
    )
    conn.execute(
        """UPDATE collector_device SET current_task_id = NULL, status = 'IDLE', updated_at = ?
           WHERE id = ? AND current_task_id = ?""",
        (now, device["id"], task_id),
    )
    conn.commit()
    task = conn.execute("SELECT * FROM scan_task WHERE id = ?", (task_id,)).fetchone()
    conn.close()
    return {"task": _mobile_task_payload(task)}


@app.post("/api/v1/device-tasks/{task_id}/fail")
def mobile_fail_task(
    task_id: str,
    request: MobileTaskActionRequest,
    authorization: Optional[str] = Header(None),
):
    device = _require_mobile_device(authorization)
    now_value = datetime.now(timezone.utc)
    now = now_value.isoformat()
    conn = mobile_conn()
    task = _require_mobile_task(conn, device["id"], task_id, request.leaseToken)
    should_retry = bool(request.retryable) and task["attempt"] < task["max_attempts"]
    next_status = "PENDING" if should_retry else "FAILED"
    available_at = (
        now_value + timedelta(seconds=max(15, task["attempt"] * 30))
    ).isoformat() if should_retry else now
    conn.execute(
        """UPDATE scan_task SET status = ?, assigned_device_id = NULL,
               lease_token = NULL, lease_expires_at = NULL, last_error = ?,
               available_at = ?, finished_at = CASE WHEN ? = 'FAILED' THEN ? ELSE NULL END
           WHERE id = ?""",
        (
            next_status,
            f"{request.errorCode}:{request.errorMessage}"[:1000],
            available_at,
            next_status,
            now,
            task_id,
        ),
    )
    conn.execute(
        """UPDATE collector_device SET current_task_id = NULL,
               status = CASE WHEN ? = 'FAILED' THEN 'FAULT' ELSE 'IDLE' END,
               last_error = ?, updated_at = ?
           WHERE id = ?""",
        (next_status, request.errorMessage[:500], now, device["id"]),
    )
    conn.commit()
    task = conn.execute("SELECT * FROM scan_task WHERE id = ?", (task_id,)).fetchone()
    conn.close()
    return {"task": _mobile_task_payload(task), "requeued": should_retry}


@app.post("/api/v1/observations/batches")
def mobile_upload_observations(
    request: MobileObservationUploadRequest,
    authorization: Optional[str] = Header(None),
):
    device = _require_mobile_device(authorization)
    now = _mobile_utc()
    conn = mobile_conn()
    accepted = 0
    duplicates = 0
    for item in request.observations:
        observation_id = str(item.get("observationId", ""))
        station_id = str(item.get("stationId", ""))
        task_id = str(item.get("taskId", "") or "")
        payload = item.get("payload", {})
        if not observation_id or not station_id:
            continue
        try:
            cur = conn.execute(
                """INSERT OR IGNORE INTO station_observation (
                       observation_id, device_id, task_id, station_id,
                       captured_at, received_at, payload
                   ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    observation_id,
                    device["id"],
                    task_id,
                    station_id,
                    str(item.get("capturedAt", now)),
                    now,
                    json.dumps(payload, ensure_ascii=False),
                ),
            )
            if cur.rowcount > 0:
                accepted += 1
            else:
                duplicates += 1
        except sqlite3.IntegrityError:
            duplicates += 1
    conn.commit()
    conn.close()
    return {"accepted": accepted, "duplicates": duplicates}


@app.get("/api/v1/admin/tasks")
def mobile_admin_tasks(
    status: Optional[str] = None,
    limit: int = Query(200, ge=1, le=1000),
):
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


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8800)
