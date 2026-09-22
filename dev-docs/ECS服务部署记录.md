# ECS 服务器部署记录与规范

> 服务器：阿里云 ECS `116.62.103.230`（实例 `iZbp1hubpamhtcenbvs1lrZ`）
> 用途：重卡充电站采集调度、数据监控与配套服务
> 最后更新：2026-09-21
> 配套文档：`cloud_service_ops.md`（采集服务运维）、`数据修改操作手册.md`（任务数据操作）

本文档记录服务器**现状**与**部署规范**，目标是：下一个服务上线时可直接照抄，避免重复踩坑。

---

## 1. 服务器环境概况

| 项 | 内容 |
|---|---|
| 系统 | Alibaba Cloud Linux（CentOS 系） |
| 规格 | 31 GiB 内存 / 60 GiB 磁盘（约 39 GiB 可用） |
| 公网 IP | `116.62.103.230` |
| 登录 | `ssh root@116.62.103.230`（本机已配置免密） |

### 已安装工具链

| 工具 | 版本 | 备注 |
|---|---|---|
| Go | **1.25.0 linux/amd64** | 可直接在服务器编译 Go 服务 |
| Nginx | 1.14.1 | 站点配置统一放在 `/etc/nginx/conf.d/` |
| Python | 3.13（`/root/miniconda3/bin/python3`） | ⚠️ **不是系统 python3**，连 MySQL 的脚本要用它 |
| sqlite3 | 有 | 查看 `amap-crawler` 调度库用 |
| docker / containerd | 运行中 | 部分服务以容器方式运行 |
| **Node.js** | ❌ **未安装** | 前端项目需**本地构建后上传 `dist/`** |

### 系统用户

- `root`、`nginx`（uid 988）
- ⚠️ **没有 `www-data` 用户**——上游文档的 systemd 模板常写 `User=www-data`，在这台机器上必须改为 `nginx`（或 root）

---

## 2. 服务清单

| 服务 | systemd 单元 | 对外端口 | 代码/数据目录 | 说明 |
|---|---|---|---|---|
| 采集调度 API | `amap-api` | `8800`（直连）+ `8082`（nginx） | `/opt/amap-crawler` | FastAPI，手机端采集调度；运维见 `cloud_service_ops.md` |
| 充电站运营监控台 | `station-dashboard` | `8090`（nginx；后端仅本地 `8088`） | `/opt/evcs-station-dashboard` | Go + Vue3，站点电价与桩状态监控（2026-09-21 部署） |
| 图片服务 | 未登记在 systemd（疑为容器/pm2） | `443` TLS → `127.0.0.1:3000` | `/opt/company-image-host` | 由 nginx `image-host.conf` 反代 |
| MCP 隧道入口 | —（由 nginx 承担） | `8081` TLS → `127.0.0.1:18081` | — | 供 AgentOS 调用本地 MCP 服务 |
| FRP 服务端 | `frps` | `7000` | `/opt/frp` | 内网穿透中继 |
| Data Pipeline Studio | `data-pipeline-studio` | `4200` | `/mnt/EVCS/data-pipeline-studio` | 由 bun 运行，非本仓库代码 |

### 端口占用总览（部署新服务前必看）

```
80    nginx  ← nginx.conf 的 default_server（⚠️ 见 §3.3）
443   nginx  ← image-host（TLS，server_name mcp.article.asia）
8081  nginx  ← MCP 隧道（TLS）
8082  nginx  ← amap-api 反代
8090  nginx  ← station-dashboard 反代        ← 2026-09-21 新增
8800  amap-api（uvicorn 直连）
8088  station-dashboard 后端（仅 127.0.0.1）
7000  frps
4200  data-pipeline-studio（bun）
22    sshd
```

---

## 3. 部署规范（新服务照此执行）

### 3.1 目录约定

- 服务代码 → `/opt/<服务名>/`
- 配置 → 服务目录下的 `.env`（`chmod 600`；属主设为运行用户，如 `chown nginx:nginx`）
- 日志 → 交给 journald（用 `journalctl -u <服务>` 查看），不要另写日志文件

### 3.2 systemd 单元模板

```ini
[Unit]
Description=<服务描述>
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=nginx                      # ⚠️ 这台机器没有 www-data
Group=nginx
WorkingDirectory=/opt/<服务名>
EnvironmentFile=/opt/<服务名>/.env
ExecStart=/opt/<服务名>/<可执行文件>
Restart=always
RestartSec=5
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
```

启用：

```bash
cp deploy/<服务>.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now <服务>
systemctl status <服务> --no-pager
```

### 3.3 ⚠️ nginx 端口规范（最容易踩的坑）

- **`80` 端口已被 `nginx.conf` 的 `listen 80 default_server` 占用。**
  新站点若直接写 `listen 80; server_name _;`，**按 IP 访问时不会命中它**（请求落到 default_server），页面打不开——**不要用 80**。
- 新服务请用**独立端口**（如监控台用 `8090`）；新增端口记得在**阿里云安全组放行**。
- 多个站点也可用明确的 `server_name`（域名）区分，共用 80。
- 后端服务建议只监听 `127.0.0.1`，由 nginx 反代，不直接暴露公网。

站点模板：

```nginx
server {
    listen 8090;                             # ← 换成分配给你的端口
    server_name _;
    root /opt/<服务名>/frontend/dist;
    index index.html;

    location / { try_files $uri $uri/ /index.html; }

    location /api/ {
        proxy_pass http://127.0.0.1:<后端端口>;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

启用：`cp xxx.conf /etc/nginx/conf.d/ && nginx -t && systemctl reload nginx`

### 3.4 前端项目（Vue / Vite 等）

后端没有 Node，**在本地构建**后只上传产物：

```bash
# 本地
cd <项目>/frontend && npm install && npm run build
rsync -az frontend/dist/ root@116.62.103.230:/opt/<服务名>/frontend/dist/
```

### 3.5 Go 项目

服务器已装 Go 1.25，直接编译即可：

```bash
cd /opt/<服务名>
go env -w GOPROXY=https://goproxy.cn,direct     # 国内拉依赖加速
go mod download
go build -o <可执行文件> .
```

### 3.6 Python 项目

统一用 miniconda 的解释器 `/root/miniconda3/bin/python3`（系统 python3 可能缺 pymysql 等依赖）。

### 3.7 数据库连接复用

直接沿用 `/opt/amap-crawler/.env` 里的 `EVCS_DATABASE_URL`（MySQL `121.41.56.201:3306/evcs`）：

```bash
grep '^EVCS_DATABASE_URL=' /opt/amap-crawler/.env > /opt/<服务名>/.env
```

> 注意：该表时间列全库按 **UTC** 存储（详见 `cloud_service_ops.md` §10.5 铁律 2）。

### 3.8 上线后检查清单

1. `systemctl is-active <服务>` → `active`
2. `journalctl -u <服务> -n 50 --no-pager` → 无报错
3. `curl http://127.0.0.1:<内部端口>/health` → 200
4. **从本机（外网）再验证一次**：`curl -o /dev/null -w '%{http_code}' http://116.62.103.230:<对外端口>/` → 确认安全组已放行
5. nginx：`nginx -t` 通过后 `systemctl reload nginx`

---

## 4. 部署实录：充电站运营监控台（2026-09-21）

**仓库**：`https://github.com/hjw-alt/evcs-station-dashboard.git`
（独立仓库；本地在 `~/workSpace/evcs-station-dashboard`，与主仓库同级）

**步骤**：

1. 上传源码（排除 `node_modules`/`dist`/`.git`）到 `/opt/evcs-station-dashboard/{backend,deploy}`
2. 本地 `npm install && npm run build` → 上传 `frontend/dist/`
3. 服务器 `go build -o station-dashboard .`
4. 生成 `.env`：复用 `EVCS_DATABASE_URL` + `DASHBOARD_ADDR=127.0.0.1:8088`
5. 安装 systemd 单元：`User=www-data` → **改为 `nginx`**
6. 安装 nginx 配置：`listen 80` → **改为 `listen 8090`**
7. 启用服务 + `nginx -t` + reload

**结果**：`http://116.62.103.230:8090/` → HTTP 200，接口返回真实数据（2521 个站点 / 18022 个充电桩 / 平均电价 0.9 元 / 分时电价覆盖 86.2%）。

**本次踩的坑（已在 §3 固化）**：

| 坑 | 现象 | 处理 |
|---|---|---|
| 80 端口被 default_server 占用 | 配置了却没生效，访问到默认站点 | 改用 8090 |
| 没有 `www-data` 用户 | 服务起不来 | `User=nginx` |
| 服务器没装 Node | 无法在服务器构建前端 | 本地构建后上传 `dist/` |

---

## 5. 待跟进

| 事项 | 说明 |
|---|---|
| 主仓库残留 `station-dashboard/` | 主仓库 `amap-charging-station-crawler` 里还留着一份同名副本（`2157ee6` 提交引入）。建议删除，统一以独立仓库为准，避免两处维护分叉 |
| 80 端口直连监控台 | 若希望 `http://116.62.103.230/` 直接打开监控台，需调整 `nginx.conf` 的 `default_server`（会影响现有默认站点） |
| 图片服务运行方式 | `/opt/company-image-host` 未登记在 systemd，疑为容器/pm2，建议确认后补录本书 |
| `data-pipeline-studio` | 运行在 `/mnt/EVCS`、端口 4200，来源与负责人待确认 |
