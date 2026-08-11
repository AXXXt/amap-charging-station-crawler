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
from datetime import datetime

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
    "host": "121.41.56.301",
    "port": 3306,
    "user": "anxitong_u",
    "password": "1d0Pb8s21d0PbLGx78Pdqqc6",
    "database": "evcs",
    "charset": "utf8mb4",
}

def get_db():
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


@app.get("/api/stations/{station_id}")
async def get_station(station_id: int):
    """查询单个充电站完整详情"""
    conn = get_db()
    if conn is None:
        return {"total": 0, "page": page, "page_size": page_size, "total_pages": 0, "data": [], "offline": True}
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
        return {"total": 0, "page": page, "page_size": page_size, "total_pages": 0, "data": [], "offline": True}
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
        return {"total": 0, "page": page, "page_size": page_size, "total_pages": 0, "data": [], "offline": True}
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

# 全局任务状态
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

def _log(msg):
    """添加日志"""
    from datetime import datetime
    task_state["log"].append(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")
    if len(task_state["log"]) > 200:
        task_state["log"] = task_state["log"][-100:]

def _run_crawl_task(cities, use_visual=False):
    """后台执行采集任务"""
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from crawler import AmapCrawler, CITY_DISTRICTS
    
    task_state["running"] = True
    task_state["error"] = None
    task_state["log"] = []
    task_state["results"] = []
    task_state["started_at"] = __import__('datetime').datetime.now().isoformat()
    
    _log(f"任务启动: {len(cities)} 个城市")
    
    try:
        # 初始化
        checker = None
        if use_visual:
            from visual_check import create_qianwen_checker
            checker = create_qianwen_checker()
            _log("视觉自检已启用 (qwen3-vl-flash)")
        
        crawler = AmapCrawler(visual_checker=checker)
        
        total_cities = len(cities)
        for ci, city in enumerate(cities):
            task_state["current_city"] = city
            _log(f"开始采集: {city}")
            
            # 按区县遍历
            districts = CITY_DISTRICTS.get(city, [city])
            
            for di, district in enumerate(districts):
                if not task_state["running"]:
                    _log("任务被用户停止")
                    break
                
                query = f"{city}{district}重卡充电站" if district != city else f"{city}重卡充电站"
                _log(f"  搜索: {query}")
                
                try:
                    stations = crawler.search_stations(city, query)
                    _log(f"  找到 {len(stations)} 个站点")
                    
                    for si, station in enumerate(stations):
                        if not task_state["running"]:
                            break
                        
                        task_state["current_station"] = station["name"][:30]
                        task_state["progress"]["done"] += 1
                        
                        try:
                            result = crawler.collect_detail(station, city)
                            if result:
                                crawler.results.append(result)
                                _log(f"    OK: {result.get('station_name', '?')[:30]}")
                        except Exception as e:
                            _log(f"    FAIL: {str(e)[:60]}")
                            
                except Exception as e:
                    _log(f"  搜索失败: {str(e)[:60]}")
            
            if not task_state["running"]:
                break
        
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
):
    """启动采集任务"""
    if task_state["running"]:
        raise HTTPException(400, "已有任务在运行，请先停止")
    
    city_list = [c.strip() for c in cities.split(",") if c.strip()]
    
    task_state["progress"] = {"done": 0, "total": 0}
    task_state["log"] = []
    
    thread = threading.Thread(target=_run_crawl_task, args=(city_list, use_visual), daemon=True)
    thread.start()
    
    return {"status": "started", "cities": city_list, "visual": use_visual}


@app.post("/api/tasks/stop")
async def stop_task():
    """停止当前任务"""
    task_state["running"] = False
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
