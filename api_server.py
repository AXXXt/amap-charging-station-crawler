"""
api_server.py — 重卡充电站数据API服务
功能：
  1. 提供RESTful API供其他平台使用
  2. 支持按城市/经纬度/运营商筛选
  3. 支持数据导出
  4. MySQL持久化存储
"""
from fastapi import FastAPI, Query, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional, List
import pymysql
import json
import os
from datetime import datetime, timezone

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


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8800)
