# 重卡充电站运营监控台

前端使用 Vue 3 + Vite + ECharts，后端使用 Go + MySQL。页面直接读取：

- `site_exploration_charging_station_result`：站点最新电价、功率、分时费率和逐桩明细
- `site_exploration_charging_station_dynamic_history`：按采集轮次生成的历史快照

## 启动后端

后端默认从当前目录或上层目录查找 `.env` 中的 `EVCS_DATABASE_URL`。

```bash
cd station-dashboard/backend
go mod tidy
go run .
```

默认监听 `http://127.0.0.1:8088`。

## 启动前端

```bash
cd station-dashboard/frontend
npm install
npm run dev
```

默认地址为 `http://127.0.0.1:5173`，Vite 会把 `/api` 代理到 Go 后端。

## API

- `GET /api/health`
- `GET /api/overview`
- `GET /api/stations?page=1&pageSize=25&sort=priceAsc`
- `GET /api/station?sourceKey=...`
- `GET /api/station/history?sourceKey=...`
- `GET /api/meta`

列表接口只返回页面需要的汇总字段，完整 `result_payload` 只在站点详情接口中返回，避免一次性把 2500 多条 JSON 全部传到浏览器。

## 服务器部署

推荐目录为 `/opt/station-dashboard`。服务器需要安装 Go 1.22+、Node.js 20+、Nginx。

```bash
sudo mkdir -p /opt/station-dashboard
sudo chown -R "$USER":"$USER" /opt/station-dashboard
git clone https://github.com/AXXXt/amap-charging-station-crawler.git /opt/station-dashboard
cd /opt/station-dashboard/station-dashboard
```

### 1. 构建后端

```bash
cd /opt/station-dashboard/station-dashboard/backend
go mod download
go build -o station-dashboard .
cp ../deploy/backend.env.example .env
```

编辑 `.env`，填写远程 MySQL 地址。生产环境建议把 `DASHBOARD_ALLOWED_ORIGIN` 设置为实际访问域名。

```bash
chmod 640 .env
chown root:www-data .env
sudo cp ../deploy/station-dashboard.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now station-dashboard
sudo systemctl status station-dashboard
```

### 2. 构建前端

```bash
cd /opt/station-dashboard/station-dashboard/frontend
npm ci
npm run build
```

### 3. 配置 Nginx

```bash
sudo cp ../deploy/nginx-station-dashboard.conf /etc/nginx/conf.d/station-dashboard.conf
sudo nginx -t
sudo systemctl reload nginx
```

访问 `http://服务器公网IP/`。页面接口统一走同域 `/api/`，不需要在前端写死服务器地址。

### 排查

```bash
curl http://127.0.0.1:8088/api/health
journalctl -u station-dashboard -n 100 --no-pager
sudo tail -f /var/log/nginx/error.log
```
