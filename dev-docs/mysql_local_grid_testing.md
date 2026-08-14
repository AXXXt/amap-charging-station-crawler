# MySQL 网格任务本地联调指南

## 1. 文档目的

本文说明如何在本机 `evcs_local_test` 数据库中验证以下链路：

```text
scan_tasks.pending
  → mysql_scan_runner.py 原子领取
  → 高德地图定位网格中心并搜索
  → crawler.py 采集详情并按网格边界过滤
  → heavy_truck_stations 写入或更新
  → collection_logs 记录结果或错误
  → scan_tasks.done / failed
```

适用范围：开发机、本地 MySQL、单台 Android 测试设备。当前实现用于安全联调，不建议直接作为远程生产任务消费者。

**版本**：1.1
**更新日期**：2026-08-14
**生成说明**：本文档由 AI 根据当前代码和实际数据库表结构生成。

## 2. 前置条件

- 本地 MySQL 8.0 已启动，监听 `127.0.0.1:3306`。
- 本地测试库名称建议为 `evcs_local_test`。
- 已在本地库创建以下三张表的结构：
  - `scan_tasks`
  - `heavy_truck_stations`
  - `collection_logs`
- Android 手机已连接，`adb devices` 显示为 `device`。
- 高德地图 App 已安装。
- 已配置高德 Web 服务 Key，用于把站点地址转换为经纬度并执行网格边界校验。
- Python 环境已安装 `pymysql`、`uiautomator2`、`requests`。

### 2.1 首次创建本地测试表

在 Navicat 的本地 MySQL 连接中新建查询，打开并执行项目文件 `dev-docs/evcs_local_test_schema.sql`。脚本会创建数据库 `evcs_local_test`（如果不存在）以及三张业务表，不会执行 `DROP`、`DELETE` 或 `TRUNCATE`。

执行后刷新 Navicat 左侧的 `evcs_local_test`，应能看到：

```sql
USE evcs_local_test;
SHOW TABLES;
SHOW CREATE TABLE heavy_truck_stations;
SHOW CREATE TABLE scan_tasks;
SHOW CREATE TABLE collection_logs;
```

如果看到 `1146 - Table 'evcs_local_test.heavy_truck_stations' doesn't exist`，说明数据库已经存在但表还没有创建；重新执行上述建表脚本并刷新对象树即可。不要只执行 `USE evcs_local_test` 或 `SHOW TABLES`，它们不会自动创建业务表。

## 3. 安全保护

`mysql_scan_runner.py` 默认实施两层保护：

1. `DB_HOST` 必须是 `127.0.0.1`、`localhost` 或 `::1`。
2. `DB_NAME` 必须为 `test`、以 `test_` 开头或以 `_test` 结尾。

因此，下面的配置会被默认拒绝：

```text
DB_HOST=远程服务器地址
DB_NAME=evcs
```

虽然命令提供 `--allow-remote` 和 `--allow-non-test-database` 开关，但本地联调阶段不要使用。真实数据库密码不得写入源码、README、截图或 Git 提交。

## 4. 配置环境变量

在运行任务的同一个 PowerShell 窗口执行：

```powershell
cd C:\Users\26381\Desktop\adb-first

$env:DB_HOST="127.0.0.1"
$env:DB_PORT="3306"
$env:DB_USER="你的本地MySQL用户"
$env:DB_PASSWORD="你的本地MySQL密码"
$env:DB_NAME="evcs_local_test"
$env:DB_CHARSET="utf8mb4"
$env:DB_CONNECT_TIMEOUT="5"

$env:DEVICE_SERIAL="你的设备序列号"
$env:ADB_PATH="C:\Android\platform-tools\adb.exe"
$env:AMAP_API_KEY="你的高德Web服务Key"
```

检查设备：

```powershell
& $env:ADB_PATH devices -l
```

## 5. 创建测试任务

在 Navicat 中连接本地 `evcs_local_test`，执行：

```sql
USE evcs_local_test;

INSERT INTO scan_tasks (
    city, district, grid_index,
    center_lng, center_lat,
    min_lng, max_lng, min_lat, max_lat,
    status
) VALUES (
    '郑州', '中原区', 900001,
    113.608932, 34.752333,
    113.588932, 113.624000, 34.732333, 34.772333,
    'pending'
)
ON DUPLICATE KEY UPDATE
    id = LAST_INSERT_ID(id),
    center_lng = VALUES(center_lng),
    center_lat = VALUES(center_lat),
    min_lng = VALUES(min_lng),
    max_lng = VALUES(max_lng),
    min_lat = VALUES(min_lat),
    max_lat = VALUES(max_lat),
    status = 'pending',
    assigned_device = NULL,
    station_count = 0,
    started_at = NULL,
    completed_at = NULL;

SELECT id, city, district, grid_index, status,
       center_lng, center_lat, min_lng, max_lng, min_lat, max_lat
FROM scan_tasks
WHERE city = '郑州'
  AND district = '中原区'
  AND grid_index = 900001;
```

`grid_index=900001` 专门用于本地测试，避免与正式网格编号混淆。示例中心点和边界仅用于验证链路；如高德搜索结果较少，可以在本地库调整边界后重新测试。

## 6. 只读检查

查看状态统计：

```powershell
python -X utf8 mysql_scan_runner.py status
```

查看指定任务：

```powershell
python -X utf8 mysql_scan_runner.py show --task-id <任务ID>
```

任务至少需要满足：

- `status='pending'`
- `city`、`district` 非空
- `min_lng`、`max_lng`、`min_lat`、`max_lat` 非空且顺序正确
- 中心点在边界内；中心点为空时由边界中点自动计算

## 7. 执行单个任务

第一次联调只运行一条明确指定的任务：

```powershell
python -X utf8 mysql_scan_runner.py run `
    --task-id <任务ID> `
    --device <设备序列号>
```

程序不会在未指定 `--task-id` 时自动消费任务。只有显式传入 `--allow-any-pending` 才会领取任意待处理任务，本地首轮测试不要使用该参数。

## 8. 状态变化

### 8.1 成功

```text
pending → scanning → done
```

`station_count` 为写入或更新的网格内站点数量。没有找到网格内站点时，`done + station_count=0` 是合法结果，不代表程序异常。

### 8.2 失败

```text
pending → scanning → failed
```

常见原因：

- 设备离线；
- 高德地图无法唤起；
- 未配置 `AMAP_API_KEY`；
- 网格边界字段缺失或无效；
- MySQL 字段、权限或连接异常；
- 高德页面结构变化导致详情采集失败。

错误信息写入：

```sql
SELECT id, station_id, layer, success, error_msg, raw_data, created_at
FROM collection_logs
ORDER BY id DESC
LIMIT 20;
```

### 8.3 手动中断

在终端按 `Ctrl+C` 时，当前任务会尝试从 `scanning` 释放回 `pending`。如果进程被强制结束，任务可能保留为 `scanning`，需要人工确认后重置。

## 9. 验证结果

执行后在 Navicat 中检查：

```sql
SELECT *
FROM scan_tasks
WHERE id = <任务ID>;

SELECT id, station_name, city, address,
       longitude, latitude, current_price,
       fast_prices, collected_at, updated_at
FROM heavy_truck_stations
ORDER BY id DESC
LIMIT 20;

SELECT id, station_id, layer, success,
       error_msg, raw_data, created_at
FROM collection_logs
ORDER BY id DESC
LIMIT 20;
```

成功任务至少会生成一条任务级 `collection_logs` 记录；每个写入或更新的站点还会生成一条站点级日志。

## 10. 重置与重复测试

使用程序重置：

```powershell
python -X utf8 mysql_scan_runner.py reset --task-id <任务ID>
```

或在确认是本地测试任务后执行：

```sql
UPDATE scan_tasks
SET status = 'pending',
    assigned_device = NULL,
    station_count = 0,
    started_at = NULL,
    completed_at = NULL
WHERE id = <任务ID>
  AND grid_index = 900001;
```

不要使用不带 `WHERE` 条件的 `UPDATE`、`DELETE` 或 `TRUNCATE`。

## 11. 当前实现边界

- 这是独立 CLI，不会由 Dashboard 或 `start.bat` 自动启动。
- 现有 `batch_runner.py` 仍使用 SQLite 单站点队列，两条任务链互不影响。
- `scan_tasks` 表没有重试次数、错误字段和租约过期字段，因此当前失败原因存放在 `collection_logs`，卡住的 `scanning` 任务需要人工重置。
- `heavy_truck_stations` 没有业务唯一索引。代码按“站名 + 城市 + 地址”查询后更新，但在多个进程并发写入同一新站点时仍可能重复。
- 网格搜索依赖高德当前地图中心和搜索推荐，最终以采集后的坐标边界过滤为准。
- 坐标缺失的站点不会被当作网格命中；必须配置 `AMAP_API_KEY` 才能运行网格任务。
- 正式接远程任务前应增加测试任务标识、租约超时、重试次数、业务唯一键、专用最小权限账号和服务级监控。

## 12. 建议测试顺序

1. 执行 `status` 和 `show`，验证数据库读取。
2. 断开手机后执行一次指定任务，验证 `failed` 和错误日志。
3. 重置任务，连接手机后执行，验证完整链路。
4. 再次重置并执行，确认站点优先更新而不是无条件新增。
5. 创建边界较小的任务，验证网格外站点不会写入。
6. 最后再考虑两台设备和多个网格并发测试。

## 13. 结论

本地联调应始终使用 `127.0.0.1` 和 `evcs_local_test`。先验证单任务、单设备、单网格的读取、执行、写入和失败恢复，再设计远程生产消费方式，可以最大限度避免误修改正式数据库和误消费正式任务。
