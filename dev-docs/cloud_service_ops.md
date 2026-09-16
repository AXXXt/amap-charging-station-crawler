# 云端采集调度服务部署与运维记录

> 面向后续独立部署、维护、排障和迁移云端采集调度服务（`api_server.py`）。

| 项目 | 内容 |
|---|---|
| 文档版本 | 2.0 |
| 更新时间 | 2026-09-16 |
| 线上基线 | 阿里云 ECS `116.62.103.230`，`/opt/amap-crawler`，`api_server.py` md5 `a42a00f640e00e66ca1e879c4f16576b`（2026-09-16 14:48 部署：**HENAN 任务权威源切换为 MySQL** + 修复手机端 claim 500，见 §10.3 / §11.7 / §13.1） |
| 当前领取模式 | `MOBILE_CLAIM_MODE=HENAN_ONLY` |
| 适用范围 | 服务部署、手机接入、任务领取策略、SQLite/MySQL 数据关系、日常运维与排障 |

## 1. 文档目的

本文件记录"云端采集调度服务"的部署形态与运维要点，使该服务可以脱离原开发电脑独立运行：

1. 明确服务部署在哪、怎么启动、怎么重启、怎么看日志。
2. 说明手机端如何接入、如何鉴权。
3. 说明任务领取策略及其可切换配置。
4. 说明 SQLite 调度库与远程 MySQL 的关系，避免"两边数据不一致"的误判。
5. 给出常见问题的排查路径。

## 2. 服务概览

`api_server.py` 是同一个 FastAPI 应用，同时承担两类职责：

| 职责 | 说明 |
|---|---|
| **手机采集调度** | 设备注册、心跳、任务领取（claim）、确认（ack）、进度、完成、失败上报 |
| **站点数据查询** | 站点列表、详情、附近站点、统计（数据源为远程 MySQL 统一结果表） |

手机端的高德地图操作与页面采集在手机本地完成，**不依赖任何电脑**；只要手机能访问云端地址，电脑即可关机。

## 3. 部署架构

```
[Android 手机采集端]
        │  HTTP（Bearer token 鉴权）
        ▼
[云服务器 ECS 116.62.103.230]
   ├── nginx :8082  ──反代──▶ uvicorn 0.0.0.0:8800  (api_server.py)
   │                              │
   │                              ├── MySQL   121.41.56.201/evcs       ← ★HENAN 任务权威源★ + 采集结果
   │                              └── SQLite  data/mobile_control.db   ← 设备/站探任务权威源（HENAN 仅启动导入缓存）
   └── systemd: amap-api.service（开机自启、崩溃自动重启）
```

## 4. 云端环境信息

| 项 | 值 |
|---|---|
| 服务器 | 阿里云 ECS `116.62.103.230` |
| 项目目录 | `/opt/amap-crawler` |
| 服务名 | `amap-api`（systemd） |
| 启动命令 | `/root/miniconda3/bin/python3 -m uvicorn api_server:app --host 0.0.0.0 --port 8800 --workers 2` |
| 监听地址 | `0.0.0.0:8800`（**必须是 0.0.0.0**，见 11.1） |
| 反向代理 | nginx `/etc/nginx/conf.d/amap-api.conf`，`listen 8082` → `proxy_pass 127.0.0.1:8800` |
| 环境变量 | `/opt/amap-crawler/.env` |
| 调度数据库 | `/opt/amap-crawler/data/mobile_control.db`（SQLite） |
| 日志 | `journalctl -u amap-api` |
| 安全组 | 入方向需放行 `TCP 8800`（手机直连）与 `TCP 8082`（nginx 入口） |

> 本目录**不是 git 仓库**，代码以 `scp` 直接上传部署；因此无法用 commit 号追溯线上版本，只能靠文件 md5 比对。

## 5. 环境变量配置（.env）

| 变量 | 说明 |
|---|---|
| `DB_HOST` / `DB_PORT` / `DB_USER` / `DB_PASSWORD` / `DB_NAME` / `DB_CHARSET` | 远程 MySQL 连接（`121.41.56.201:3306/evcs`） |
| `MOBILE_ACTIVATION_CODE` | 手机端激活码 |
| `MOBILE_ADMIN_API_KEY` | 管理接口密钥 |
| `MOBILE_LEASE_SECONDS` | 任务租约时长（秒），默认 600 |
| `MOBILE_LEASE_RECLAIM_SECONDS` | 过期租约回收判断间隔 |
| `MOBILE_CLAIM_MAX_RETRIES` | 领取重试次数，默认 3 |
| `HENAN_FAILED_RETRY_LIMIT` | 河南 POI 任务补采上限，默认 2 |
| **`MOBILE_CLAIM_MODE`** | **任务领取模式：`LEGACY`（默认）/ `HENAN_ONLY`** |
| **`HENAN_MYSQL_AUTHORITATIVE`** | **HENAN 任务权威源开关：`1`（当前值，也是代码默认）= MySQL 权威；`0` = 回退旧的"SQLite 权威"模式（改完需重启）** |
| `SITE_EXPLORATION_TASKS_ENABLED` | 是否启用站点探索任务源（远程桥接） |
| `AMAP_API_KEY` | 高德接口 Key（如配置了 IP 白名单，需加入 ECS 公网 IP） |

## 6. 任务领取逻辑

接口：`POST /api/v1/device-tasks/claim`

### 6.1 优先级

| 优先级 | 条件 | 说明 |
|---|---|---|
| 1 | `HENAN_POI_DETAIL` + `PENDING` 且 `attempt < max_attempts` | 正常待采集（按 `source_sequence, created_at, id` 排序） |
| 2 | `HENAN_POI_DETAIL` + `FAILED` 且 `recovery_attempt < max_recovery_attempts` | 失败补采 |
| — | 以上都没有 | 返回 `{"task": null, "reason": "QUEUE_EMPTY"}`，手机端显示「等待任务中」 |

### 6.2 领取模式（`MOBILE_CLAIM_MODE`）

| 模式 | 行为 |
|---|---|
| `LEGACY`（默认，不配置时） | 保留历史行为：河南任务无任务时，**回退领取** `SITE_STATION_DETAIL` / `REGION_SCAN` |
| `HENAN_ONLY` | **只领河南 POI 任务**（PENDING 优先 → 失败补采），两类都没有直接返回无任务，不再回退 |

切换方式（无需改代码）：

```bash
# 改为只领河南 POI
ssh root@116.62.103.230 "sed -i 's/^MOBILE_CLAIM_MODE=.*/MOBILE_CLAIM_MODE=HENAN_ONLY/' /opt/amap-crawler/.env && systemctl restart amap-api"

# 切回历史行为
ssh root@116.62.103.230 "sed -i 's/^MOBILE_CLAIM_MODE=.*/MOBILE_CLAIM_MODE=LEGACY/' /opt/amap-crawler/.env && systemctl restart amap-api"
```

### 6.3 补充规则

- **补采上限**：任务失败后进入补采，`recovery_attempt` 达到 `max_recovery_attempts`（默认 2）后不再自动重试，需要人工重置才会重跑。
- **波次等待**：若河南任务仍有 `LEASED`/`RUNNING`（`recovery_attempt = 0`）的任务在其他设备上执行，且无待领 PENDING 时，返回无任务，等主任务结束后再进入补采波次。
- **续租**：设备已持有且租约未过期的任务，会在下次 claim 时原样返回（`reason = CURRENT_TASK`）。

## 7. 手机端接入

| 项 | 值 |
|---|---|
| 服务地址 | `http://116.62.103.230:8800`（直连）或 `http://116.62.103.230:8082`（nginx 反代，功能等价） |
| 鉴权方式 | `Authorization: Bearer <token>`，token 落库于 SQLite `collector_device.token_hash` |
| 设备标识 | `device_code`（如 `EV-92F391`），claim 时校验一致性 |

> 迁移时只要把 SQLite 中的 `collector_device` 表一并迁移，手机上已保存的 token 依然有效，**无需重新激活**。

## 8. 接口清单

| 方法 | 路径 | 用途 |
|---|---|---|
| POST | `/api/v1/devices/register` | 设备注册/激活 |
| POST | `/api/v1/devices/heartbeat` | 设备心跳 |
| GET | `/api/v1/devices/me/config` | 设备配置下发 |
| POST | `/api/v1/device-tasks/claim` | 领取任务 |
| POST | `/api/v1/device-tasks/{id}/ack` | 确认收到任务 |
| POST | `/api/v1/device-tasks/{id}/progress` | 上报进度 |
| POST | `/api/v1/device-tasks/{id}/complete` | 上报完成 |
| POST | `/api/v1/device-tasks/{id}/fail` | 上报失败 |
| POST | `/api/v1/device/adb-tap` | 远程 tap（调试用） |
| GET | `/api/v1/admin/tasks` | 任务查询（管理，需 admin key） |
| GET | `/` | 健康检查 |

## 9. 日常运维命令

```bash
# 状态 / 重启 / 日志
ssh root@116.62.103.230 "systemctl status amap-api"
ssh root@116.62.103.230 "systemctl restart amap-api"
ssh root@116.62.103.230 "journalctl -u amap-api -n 50 --no-pager"

# 线上代码校验（与本地比对）
ssh root@116.62.103.230 "md5sum /opt/amap-crawler/api_server.py"

# 任务状态分布 + 在途任务（★权威源 = MySQL，HENAN 任务只看这里）
ssh root@116.62.103.230 'cd /opt/amap-crawler && python3 -c "
import re,pymysql
u=re.search(r\"^EVCS_DATABASE_URL=(\S+)\",open(\".env\").read(),re.M).group(1).strip().strip(chr(34))
m=re.match(r\"mysql://([^:]+):([^@]+)@([^:/]+):(\d+)/(\S+)\",u)
c=pymysql.connect(host=m.group(3),port=int(m.group(4)),user=m.group(1),password=m.group(2),database=m.group(5),charset=\"utf8mb4\")
cur=c.cursor()
cur.execute(\"SELECT status,COUNT(*) FROM henan_heavy_truck_charging_station_task GROUP BY status\"); print(\"STATUS\", cur.fetchall())
cur.execute(\"SELECT lease_device_id,status,COUNT(*) FROM henan_heavy_truck_charging_station_task WHERE status IN (%s,%s) GROUP BY lease_device_id,status\",(\"LEASED\",\"RUNNING\")); print(\"INFLIGHT\", cur.fetchall())"'

# 非 HENAN 任务（站探/区域扫描）与设备仍在 SQLite（这些仍以 SQLite 为权威源）
ssh root@116.62.103.230 "sqlite3 /opt/amap-crawler/data/mobile_control.db \
  \"SELECT type, status, COUNT(*) FROM scan_task GROUP BY type, status;\""

# 服务是否被重启过（核对"启动导入"是否刷过进度）
ssh root@116.62.103.230 "systemctl show amap-api -p ExecMainStartTimestamp --no-pager"
```

## 10. 数据存储说明

### 10.1 SQLite `data/mobile_control.db`（设备/站探任务权威源；HENAN 仅启动导入缓存）

| 表 | 用途 |
|---|---|
| `scan_task` | 任务队列（含状态、租约、尝试次数、补采次数） |
| `collector_device` | 采集设备与 token |
| `station_observation` | 采集结果观察记录 |

`scan_task` 关键列：`id`、`type`、`status`、`attempt` / `max_attempts`、`recovery_attempt` / `max_recovery_attempts`、`assigned_device_id`、`lease_token`、`lease_expires_at`、`available_at`、`priority`、`source_sequence`、`last_error`。

> ⚠️ 2026-09-16 起：`scan_task` 里的 **HENAN_POI_DETAIL 行只是服务启动时导入 MySQL 的缓存**，运行期不再被更新（`_sync_henan_task()` 已是空实现）。查 HENAN 任务进度请看 §10.2 / §10.3。

> 时间字段为 **ISO 8601 UTC 字符串**（`2026-09-15T02:49:52.140+00:00`），比较用字符串排序。

### 10.2 远程 MySQL `121.41.56.201/evcs`

| 表 | 用途 |
|---|---|
| `henan_heavy_truck_charging_station_task` | 河南 POI 任务**权威源**（手机端 claim/ack/progress/complete/fail 直读直写；仅在服务启动时由 SQLite 导入） |
| `site_exploration_charging_station_result` | 采集结果统一表（站点查询接口数据源） |
| `heavy_truck_stations` | 历史站点表（已由统一结果表替代） |

### 10.3 权威源与同步方向（2026-09-16 已反转，重要）

```
服务启动时：  SQLite scan_task ──全量 UPSERT──▶ MySQL henan_heavy_truck_charging_station_task
运行期间：    手机端 ──事务 + 行锁 + 租约校验──▶ MySQL（权威；SQLite 不再参与）
```

**HENAN 任务的权威源是 MySQL，SQLite 只是「启动导入缓存」。** 因此：

- 手机端 claim/ack/progress/complete/fail 全部直连 MySQL，用 `FOR UPDATE` 行锁 + `lease_token`/`lease_expires_at` 租约校验保证并发安全；过期租约由 `_henan_mysql_reap_expired()` 回收。
- **改状态要改 MySQL**（立即生效）；但**只改 MySQL 会在下次重启时被 SQLite 快照覆盖**，所以两个库必须一起改（见 §10.5），或直接用 `reset_tasks.py --apply`。
- `_sync_henan_task()` 已改为空实现（`return None`）：**旧的"镜像写失败不影响采集"兜底已不存在**，MySQL 被长事务锁住时手机端请求会直接 500（见 §11.7）。
- **重启副作用**：启动导入会用 SQLite 快照覆盖 MySQL 运行期状态（`status`/`attempt` 等），即"重启把进度刷回去"。正在大批量采集时不要重启。
- **回退方式**：`.env` 设 `HENAN_MYSQL_AUTHORITATIVE=0` 并重启，即恢复旧的"SQLite 权威"行为。

### 10.4 重置任务为待领取（示例）

> ⚠️ 新架构下：下面这条**只改 SQLite** 的语句当前**不会立即生效**（HENAN 已不从 SQLite 读），要等服务重启经启动导入才生效。**推荐直接用 §10.5 的 `reset_tasks.py --apply`（两个库一起改）**；若用手工 SQL，请按"**MySQL 先改（立即生效）+ SQLite 后改（防重启回滚）**"两步走。

```sql
UPDATE scan_task
   SET status='PENDING', attempt=0, recovery_attempt=0,
       assigned_device_id=NULL, lease_token=NULL, lease_expires_at=NULL,
       started_at=NULL, finished_at=NULL, last_error='',
       progress='{}', result_summary='{}',
       available_at=strftime('%Y-%m-%dT%H:%M:%f+00:00','now'),
       updated_at=strftime('%Y-%m-%dT%H:%M:%f+00:00','now')
 WHERE id IN (
   SELECT id FROM scan_task
    WHERE type='HENAN_POI_DETAIL' AND status='COMPLETED'
    ORDER BY RANDOM() LIMIT 2          -- 按需替换筛选条件
 );
```

### 10.5 批量重置任务状态（重跑采集）

> 场景：把已完成的任务重新跑一遍（`COMPLETED` → `PENDING`）。脚本 `reset_tasks.py` 随代码同步到 `/opt/amap-crawler`。

**两条铁律（2026-09-16 已改写，先看再用）**

1. **权威源是 MySQL，SQLite 是启动导入缓存**（§10.3）。脚本会**两个库一起改**——先备份并重置 SQLite（防重启回滚），再按 `local_task_id` 把状态写进 MySQL（**这一步才真正生效**）。
2. **"镜像故障兜底"已经没有了**：`_sync_henan_task()` 已是空实现，MySQL 一旦被长事务锁住（`1205 Lock wait timeout`），手机端 claim/ack/progress/complete/fail 会**直接 500、采集中断**，没有降级路径，只能先杀掉长事务解锁。

**用法**（在 ECS 上 `cd /opt/amap-crawler`）

```bash
python3 reset_tasks.py                                        # dry-run（默认）：只统计，不写任何库
python3 reset_tasks.py --apply                                # 重置 COMPLETED → PENDING（自动备份 SQLite）
python3 reset_tasks.py --apply --include-failed               # 连 FAILED（补采）一起归零
python3 reset_tasks.py --apply --types HENAN_POI_DETAIL,SITE_STATION_DETAIL
python3 reset_tasks.py --apply --mirror-only                  # 只改 MySQL（立即生效，但 SQLite 仍旧值 → 下次重启会回滚）
python3 reset_tasks.py --apply --skip-mirror                  # 只改 SQLite（当前不生效，等重启导入才生效）
```

**脚本做了什么**

- 默认 dry-run，必须显式 `--apply` 才写库；
- 写 SQLite 前用 `sqlite3 .backup` 生成 `<db>.bak-<时间戳>`（WAL 下也安全）；
- SQLite 侧单事务（`BEGIN IMMEDIATE`）+ 更新行数校验；
- MySQL **权威源**分批（默认 100 行/批、独立事务、5 秒锁等待）+ 被锁行跳过 + 末尾汇总剩余；
- 退出码：`0` 全部完成；`1` 有部分 MySQL 行未完成（重跑 `--apply` 补齐；注意此时 SQLite 已改，重启会把它刷进 MySQL）。

**遇到 1205 锁**

```sql
-- 需要带 PROCESS / SUPER 权限的账号（evcs_u 无此权限）
SELECT trx_id, trx_mysql_thread_id, TIMESTAMPDIFF(SECOND, trx_started, NOW()) age_s, LEFT(trx_query, 80)
  FROM information_schema.INNODB_TRX ORDER BY age_s DESC;
KILL <trx_mysql_thread_id>;
```

常见原因：有人在图形客户端（Navicat / DBeaver 之类）改过这张表但没有提交，持有大量行锁 —— 表现为 SELECT 正常、任何 UPDATE 都超时、DDL 也被挡。
**⚠️ 这张表现在是权威源：锁住它等于直接掐断手机端采集**（不再是"只影响镜像同步"，也没有兜底）。

**回滚**

```bash
systemctl stop amap-api
cp /opt/amap-crawler/data/mobile_control.db.bak-<时间戳> /opt/amap-crawler/data/mobile_control.db
rm -f /opt/amap-crawler/data/mobile_control.db-wal /opt/amap-crawler/data/mobile_control.db-shm
systemctl start amap-api
```

**实操留痕（2026-09-16）**

| 时间 | 操作 | 备份 / 备注 |
|---|---|---|
| 13:55 | `HENAN_POI_DETAIL` 2343 条 `COMPLETED` → `PENDING`（限类型、清空领取痕迹） | `data/mobile_control.db.bak-20260916-1355` |
| 14:02 | 复查 `LEASED`/`RUNNING` 均为 0 后才动手；期间重启过一次服务 | — |
| 14:1x | `_sync_henan_task()` 增加镜像失败兜底（止血：500 → 0）。**该兜底已于 14:38 随权威源切换被移除** | `api_server.py.bak-20260916` |
| 14:38 | 部署 `10e5eab`：HENAN 任务权威源切换为 MySQL；重启时导入 2351 条任务 | 提交 `10e5eab` |
| 14:39–14:45 | ⚠️ 事故：手机端 claim 连续 500（`json.loads(dict)` 类型错误），采集中断 | 见 §11.7 |
| 14:48 | 部署修复 `_payload_object()`；服务恢复、Traceback 归零 | `api_server.py.bak-20260916-1448`（md5 `a42a00f640e00e66ca1e879c4f16576b`） |

## 11. 常见问题排查

### 11.1 公网连不上 8800（安全组已放行）

**根因**：`ExecStart` 中 `--host 127.0.0.1` 只监听本机回环，外部无法访问。
**排查**：`ss -tlnp | grep 8800`，若显示 `127.0.0.1:8800` 而非 `0.0.0.0:8800` 即为该问题。
**修复**：把 systemd 的 `--host` 改为 `0.0.0.0` 后 `daemon-reload` + 重启。

> 注意：用 `python3 -m uvicorn api_server:app` 启动时，`api_server.py` 末尾的 `uvicorn.run(app, host="0.0.0.0", ...)` **不会执行**，以 systemd 命令行为准。

### 11.2 手机能连上但提示无任务

检查两类可领任务是否为 0（★权威源 = MySQL，**不要查 SQLite**）：

```bash
# 第 1 优先：待采集　第 2 优先：未达上限的失败补采
ssh root@116.62.103.230 'cd /opt/amap-crawler && python3 -c "
import re,pymysql
u=re.search(r\"^EVCS_DATABASE_URL=(\S+)\",open(\".env\").read(),re.M).group(1).strip().strip(chr(34))
m=re.match(r\"mysql://([^:]+):([^@]+)@([^:/]+):(\d+)/(\S+)\",u)
c=pymysql.connect(host=m.group(3),port=int(m.group(4)),user=m.group(1),password=m.group(2),database=m.group(5),charset=\"utf8mb4\")
cur=c.cursor()
cur.execute(\"SELECT COUNT(*) FROM henan_heavy_truck_charging_station_task WHERE status=%s AND attempt < max_attempts\",(\"PENDING\",)); print(\"PENDING 可领\", cur.fetchone())
cur.execute(\"SELECT COUNT(*) FROM henan_heavy_truck_charging_station_task WHERE status=%s AND recovery_attempt < max_recovery_attempts\",(\"FAILED\",)); print(\"FAILED 可补采\", cur.fetchone())"'
```

两者都为 0 时返回「等待任务中」属正常；如需补充待领取任务，按 §10.5 用 `reset_tasks.py --apply` 重置，或导入新的 POI 任务。

### 11.3 服务起不来

```bash
journalctl -u amap-api -n 50 --no-pager
```

多数为 `.env` 中 MySQL 连接信息错误，或数据库表结构缺失。

### 11.4 鉴权 401

手机端 token 与 `collector_device.token_hash` 不匹配（例如迁移时漏迁设备表），需重新激活设备。

### 11.5 逆地理编码失败

高德 Key 若配置了 IP 白名单，需把 ECS 公网 IP 加入白名单。

### 11.6 启动较慢（约 50 秒）

`startup` 阶段会执行：初始化 SQLite、**把全部河南任务从 SQLite 全量 UPSERT 导入 MySQL（约 50 秒）**、同步站点探索任务、启动租约回收线程。启动完成后端口才开始接受连接。

> ⚠️ 这一步会**用 SQLite 快照覆盖 MySQL 中运行期产生的状态**（`status`/`attempt` 等），即"重启把进度刷回去"。正在大批量采集时不要重启。

### 11.7 手机端 claim/ack 整片 500：`json.loads(dict)` 类型错误（2026-09-16 已修）

**现象**：`POST /api/v1/device-tasks/claim` 返回 500，手机端采集中断。日志堆栈：

```
File "/opt/amap-crawler/api_server.py", line 1939, in mobile_claim_task
File "/opt/amap-crawler/api_server.py", line 1340, in _mobile_task_payload
TypeError: the JSON object must be str, bytes or bytearray, not dict
```

**根因**：权威源切到 MySQL 后，`_henan_mysql_row_to_task()` 用 DictCursor 读库，JSON 列（`progress`/`result_summary`）已被解码成 `dict`；而 `_mobile_task_payload()` 仍按 SQLite 的约定无条件 `json.loads()`。该函数是**所有手机端接口的唯一出口**（18 处调用），所以 claim/ack/progress/complete/fail 会一起崩。

**触发条件很隐蔽**：任务 `progress` 为空时正常，**一旦手机上报过进度就必崩** —— 表现为"前几次 200、之后全是 500"。

**修复**：新增 `_payload_object()` 兼容"JSON 字符串 / 已解码 dict / None"，替换三处无条件 `json.loads()`（`_mobile_task_payload` 的 `progress`、`resultSummary`，以及 `_mobile_device_payload` 的 `capabilities`）。

**同类问题排查判据**：凡是从行对象读 JSON 列的地方（`grep -n "json.loads" api_server.py`），都要确认行来源是 SQLite（字符串）还是 MySQL DictCursor（已解码）。

## 12. 部署与迁移步骤（复现）

```bash
# 1. 同步代码到 /opt/amap-crawler
# 2. 迁移调度库（关键，含任务状态与设备 token）
scp data/mobile_control.db root@<IP>:/opt/amap-crawler/data/
# 3. 配置 .env（MySQL 连接、激活码、管理密钥、领取模式）
# 4. 装载 systemd 服务并启动
systemctl daemon-reload && systemctl enable --now amap-api
# 5. 云控制台安全组放行 TCP 8800 / 8082
# 6. 手机端把服务地址改为 http://<IP>:8800
```

**迁移前**应先让手机把本地未上传结果同步完成，避免"一边未传、一边已迁移"的数据缺口。

## 13. 变更记录

### 13.1 2026-09-16：HENAN 任务权威源切换为 MySQL（提交 `10e5eab`）

| 变更 | 说明 |
|---|---|
| **权威源切换** | HENAN 任务调度权威源由 ECS SQLite 改为远程 MySQL `henan_heavy_truck_charging_station_task`；SQLite 降级为启动导入缓存 |
| **运行期回写移除** | `_sync_henan_task()` 改为空实现（`return None`），运行期不再把状态写回 SQLite；同步只剩启动时一次全量 UPSERT |
| **并发安全** | claim/ack/progress/complete/fail 改为 MySQL 事务 + `FOR UPDATE` 行锁 + 租约校验（`lease_token`/`lease_expires_at`），过期租约由 `_henan_mysql_reap_expired()` 回收 |
| **新增开关** | `.env` 的 `HENAN_MYSQL_AUTHORITATIVE`（默认 `1`；设 `0` 回退旧的 SQLite 权威模式，需重启） |
| **兜底移除** | 原"镜像写失败不影响采集"的兜底消失 → MySQL 被长事务锁住时手机端直接 500，无降级路径 |
| **附带风险（待评估）** | 重启会用 SQLite 快照覆盖 MySQL 运行期状态，即"重启回滚进度"。建议后续改为"只 INSERT 缺失任务、不覆盖已有任务的 status" |
| **缺陷修复** | `_mobile_task_payload()` 的 `json.loads(dict)` 类型错误（导致 claim/ack/progress/complete/fail 全片 500），改为 `_payload_object()` 兼容两种行来源（见 §11.7） |

**影响面**：手机端接口行为不变（同一套 URL 与鉴权），但**运维方式变了** —— 任务状态权威源、查询入口、重置流程都要按 §10.3 / §10.5 执行。

### 13.2 2026-09-15

| 变更 | 说明 |
|---|---|
| 调度服务上云 | SQLite 调度库迁移至 ECS，任务/设备状态完整接续 |
| 监听修正 | systemd `--host` 由 `127.0.0.1` 改为 `0.0.0.0`，手机可直连 8800 |
| 领取逻辑调整 | 新增 `MOBILE_CLAIM_MODE`，支持"只领河南 POI"（`HENAN_ONLY`） |
| 数据源切换 | 站点查询接口统一改为 `site_exploration_charging_station_result` |

线上备份文件：

| 文件 | 说明 |
|---|---|
| `/opt/amap-crawler/api_server.py.bak-before-claim-change` | 领取逻辑改造前的服务端代码 |
| `/opt/amap-crawler/data/mobile_control.db.empty.bak` | 迁移前 ECS 上的空调度库 |
| `/opt/amap-crawler/data/mobile_control.db.bak-before-reset` | 任务重置前的调度库快照 |
