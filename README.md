# 高德地图重卡充电站数据采集系统

通过 ADB + uiautomator2 自动化采集高德地图 App 中河南省重卡充电站的详情数据，并提供 RESTful API 供其他平台调用。

## 功能概览

### 1. 数据采集 (`crawler.py`)

- **区县粒度搜索**：按 `{城市}{区县}重卡充电站` 遍历河南省 18 市、157 个区县配置项
- **自适应页面类型**：自动识别 4 种详情页并采用不同采集策略

| 页面类型 | 特征 | 策略 |
|---------|------|------|
| `basic` | 仅名称+地址 | dump 1次 |
| `standard` | 含设备/停车费 | 滚动1次 + dump合并 |
| `full_trend` | 含24h价格趋势图 | 每屏动态检测趋势入口；被底部操作栏遮挡时继续滚动，安全可见后点击详情并合并多屏数据 |
| `click_to_expand` | 需点击电价查看分时 | 点击价格卡片进入详情页，状态机解析后返回原站点并继续采集 |

- **采集字段**：
  - 站点名称、营业状态、详细地址、运营商
  - 实时电价、24小时分时电价（统一包含参考价、电费、服务费、标签和数据来源）
  - 设备信息：快充/慢充/超充 可用数/总枪数/功率
  - 停车费、占位费、设施标签、收藏数
  - 高德 API 逆地理编码获取经纬度
- **分时电价采集**：初始页及每次滚动后的 XML 都会重新检测价格入口；有电价或设备信号的页面最多探测 3 次，价格详情页数据优先，内嵌趋势兜底
- **遮挡保护**：趋势图刚出现在屏幕底部但仍被悬浮导航栏遮挡时不点击，继续滚动到安全可见区域后再进入详情
- **页面强校验**：点击价格入口后必须识别为 `PRICE_DETAIL`；返回到详情低位时会向上恢复并确认原站点名称，避免误点或返回错误页面
- **区县收敛**：搜索卡片有明确行政区地址时先做预过滤；地址分为目标区、明确跨区和未知三态，未知地址仍进入详情核验且不触发停止。已命中目标区后若连续出现 4 个明确跨区结果，结束当前区县滚动，避免高德持续推荐全市其他区域站点
- **去重**：采集完成后按坐标距离自动去重（同名<500m / 异名<100m）
- **搜索框自适应**：自动识别首页/搜索页/地图三种状态，使用对应搜索入口

### 2. 数据服务 (`api_server.py`)

FastAPI 服务，端口 8800：

| 接口 | 说明 |
|------|------|
| `GET /api/stations` | 分页查询，支持城市/运营商筛选 |
| `GET /api/stations/{id}` | 单站点详情 |
| `GET /api/stations/nearby?lng=&lat=&radius=` | 附近站点搜索 |
| `GET /api/stats` | 统计概览（各城市/运营商分布） |
| `POST /api/stations/batch` | 批量导入数据 |

数据存储于 MySQL，表 `heavy_truck_stations` 自动建表。

### 3. 视觉自检 (`visual_check.py`)

- `VisualChecker`：截图 → 判断页面状态 → 自动弹窗关闭/误触恢复
- `VisualModelAdapter`：用户自定义视觉模型接入接口
- `check_text_fallback()`：无视觉模型时的纯文本兜底
- 可一键注入到 `crawler.py` 采集流程中

**已集成千问免费视觉模型**（`qwen3-vl-flash`）：
```python
from visual_check import create_qianwen_checker
from crawler import AmapCrawler

checker = create_qianwen_checker()       # 自动使用千问视觉模型
crawler = AmapCrawler(visual_checker=checker)
crawler.run_city("郑州")                  # 采集时自动弹窗检测+恢复
```

也可使用自定义视觉模型：
```python
from visual_check import VisualModelAdapter

def my_model(img_path):
    return {"page_type": "detail", "has_popup": False}

checker = VisualChecker(d, visual_model_func=VisualModelAdapter(my_model))
```

## 环境要求

- Python 3.10+
- Android 手机 + ADB 调试已开启
- uiautomator2 已安装并初始化
- 高德地图 App 已安装

```bash
pip install uiautomator2 fastapi uvicorn pymysql requests
```

## 环境变量

敏感配置不写入代码仓库。PowerShell 示例：

```powershell
$env:DEVICE_SERIAL="你的设备序列号"
$env:ADB_PATH="C:\Android\platform-tools\adb.exe"
$env:AMAP_API_KEY="你的高德 Web 服务 Key"
$env:DASHSCOPE_API_KEY="你的千问 API Key"  # 可选，仅视觉异常审查使用

$env:DB_HOST="127.0.0.1"
$env:DB_PORT="3306"
$env:DB_USER="evcs_app"
$env:DB_PASSWORD="你的数据库密码"
$env:DB_NAME="evcs"
```

未配置 MySQL 时，采集与 SQLite 调度仍可运行；数据库类 API 会返回 `503`。

## 快速开始

### 1. 连接设备

```bash
adb devices
# 应显示设备序列号，如 RFCXA0W194D
```

设置 `DEVICE_SERIAL`，或在批处理命令中通过 `--devices` 指定设备序列号。

### 2. 单城市测试

```bash
python crawler.py
# 默认采集郑州市，结果保存到 collected_data.json
```

### 3. 全省采集

编辑 `crawler.py`，取消 `run_all()` 的注释：
```python
if __name__ == "__main__":
    crawler = AmapCrawler()
    crawler.run_all()                           # 全省采集
    crawler.deduplicate_results()               # 去重
    crawler.save_results("henan_stations.json") # 保存
```

### 4. 启动 API 服务

推荐直接双击项目根目录的 `start.bat`：

- 自动启动端口 `8800` 的后端服务
- 健康检查通过后自动打开控制台
- 按任意键只停止本项目后端，不会结束其他 Python 程序
- 启动日志：`logs/api_server.log`
- 错误日志：`logs/api_server_error.log`

也可以在终端中手动启动：

```bash
python api_server.py
# 服务运行在 http://localhost:8800
```

### 5. 导入数据到 MySQL

设置上文 `DB_*` 环境变量，然后：

```python
import json, requests

with open("collected_data.json", encoding="utf-8") as f:
    data = json.load(f)

resp = requests.post("http://localhost:8800/api/stations/batch", json=data["stations"])
print(resp.json())  # {"inserted": N, "total": M}
```

### 6. 接入视觉模型（可选）

```python
from visual_check import VisualModelAdapter, integrate_with_crawler
from crawler import AmapCrawler

def my_visual_model(image_path):
    # 调用你的视觉模型
    return {"page_type": "detail_page", "has_popup": False}

crawler = AmapCrawler()
adapter = VisualModelAdapter(my_visual_model)
integrate_with_crawler(crawler, adapter)
crawler.run_all()
```

## 目录结构

```
adb-first/
├── crawler.py          # 采集引擎
├── api_server.py       # FastAPI 数据服务
├── visual_check.py     # 视觉模型自检模块
├── batch_test_v2.py    # 端到端测试脚本
├── .gitignore
└── README.md
```

## 河南城市覆盖

18 个城市、157 个区县配置项，自动遍历：

郑州、洛阳、开封、南阳、许昌、平顶山、新乡、安阳、焦作、商丘、周口、驻马店、信阳、漯河、三门峡、鹤壁、濮阳、济源

## 数据样例

```json
{
  "station_name": "特来电汽车充电站(特来电郑州惠济颂慧充电站)",
  "operator": "特来电",
  "address": "郑州市惠济区...",
  "current_price": "1.35",
  "fast_available": "11",
  "fast_total": "26",
  "fast_power": "120.0kW|750V",
  "longitude": 113.475943,
  "latitude": 34.883285,
  "price_schedule_source": "price_detail",
  "price_detail_attempted": true,
  "price_detail_collected": true,
  "fast_prices": [
    {
      "time": "00:00-07:00",
      "total_price": "0.64",
      "elec_fee": "0.37",
      "service_fee": "0.27",
      "tag": "最低",
      "source": "price_detail"
    },
    {
      "time": "07:00-16:00",
      "total_price": "0.88",
      "elec_fee": "0.58",
      "service_fee": "0.30",
      "tag": "",
      "source": "price_detail"
    }
  ]
}
```
