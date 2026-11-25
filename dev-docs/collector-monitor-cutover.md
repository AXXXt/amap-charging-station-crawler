# 采集监控台替换网页入口（2026-11-25）

本次发布更新采集 API 和百度/高德采集 App，把原 station-dashboard 的网页入口切到新版采集监控台。登录密码按需求设置为 **123456**。原 station-dashboard Go 后端、日报生成服务和 EVCS 接口继续按原配置运行。

## 发布边界

| 项目 | 本次操作 |
| --- | --- |
| amap-api，现有目录 /opt/amap-crawler，端口 8800 | 更新下列成套文件，设置一个监控密码变量，重启此服务 |
| 原 station-dashboard 的 Nginx server | 保留监听端口、域名、TLS、root、原 /api/ 反代和其他路由，仅增加新版页面与监控 API 路由 |
| station-dashboard Go 服务，端口 8088 | 保持运行，不停止、不重启、不替换二进制或配置 |
| 日报与 EVCS | 保留现有数据库、定时任务、模型配置、接口及日报名单；不触发历史重生成 |
| 手机 | 更新百度 0.1.75（versionCode 76）、高德 0.1.42.28（versionCode 70），保留 App 数据 |
| 正式任务 | 百度 1080、高德 138，共 1218；不再次迁移、不重建批次、不清进度 |

日报服务原目录为 /opt/evcs-station-dashboard/backend，其环境文件、数据库连接和冻结的 1146 站日报范围与采集的 1218 站是两套用途，均保留现状。

## 1. 固定版本与发布包

后端/监控台/高德源码仓库：AXXXt/amap-charging-station-crawler，分支 codex/collector-monitor-device-control-20261125。
百度源码仓库：hjw-alt/collector-baidu，分支 codex/baidu-monitor-control-20261125。

以本次交接给出的完整 commit 固定发布，不使用未来可能变化的分支 HEAD。现有生产目录历史上使用 scp 发布，不能假设它是 Git 仓库。在开发机的后端仓库执行以下命令打包；REV 填本次后端的完整 commit：


```bash
REV='<本次后端完整commit>'
git archive --format=tar.gz --output=collector-monitor-server-20261125.tar.gz "$REV" \
  api_server.py collector_monitor.py device_control.py fleet_control.py \
  monitor_scheduler.py cross_platform_monitor.py priority_station_scopes.py \
  config/priority_station_scopes.json collector-monitor \
  dev-docs/collector-monitor-cutover.md dev-docs/nginx-station-dashboard-locations.conf \
  dev-docs/releases/20261125-collector-monitor/README.md
sha256sum collector-monitor-server-20261125.tar.gz
```

包内没有 .env、数据库、设备 token 或模型密钥。前一版“百度/高德主任务各自开新轮，上一轮补采继续”的四个文件已包含在上述清单中，无需再部署一个互相覆盖的旧包。没有新增 Python 依赖。

## 2. 记录旧服务状态并备份

先记录原 station-dashboard 的 MainPID、ActiveState、ExecMainStartTimestamp；部署后应保持一致。备份实际生效的原 Nginx server 配置，并用只读请求记录旧接口的响应。不要调用 POST /api/ai-report/generate。


```bash
systemctl show station-dashboard -p MainPID -p ActiveState -p ExecMainStartTimestamp
curl --fail --silent --show-error http://127.0.0.1:8088/api/health
curl --fail --silent --show-error http://127.0.0.1:8088/api/ai-report/dates
```

备份 /opt/amap-crawler 下将覆盖的文件、现有采集 .env 和 MOBILE_DB_PATH 指定的 SQLite。SQLite 使用 sqlite3 的 .backup 或等价在线备份，不能在运行时只拷贝主 db 而遗漏 WAL。数据库备份留在服务器私有目录，不放进源码或分享包。保留当前虚拟环境、服务账号、启动参数和原权限。

## 3. 只更新采集服务

将发布包解到临时目录，按清单复制文件到 /opt/amap-crawler，collector-monitor/ 中必须同时包含 index.html、monitor.js、monitor.css、public-sans.woff2。不要用新 checkout 或示例配置覆盖整个运行目录。

在 amap-api 实际运行环境设置：


```bash
COLLECTOR_MONITOR_KEY=123456
```

程序会读取现有 /opt/amap-crawler/.env；若 systemd 已有同名变量，以 systemd 环境为准。只增加/修改这个变量，保留数据库、派发模式、设备控制库路径及其他配置。不要复制 .env.example 到生产。


```bash
cd /opt/amap-crawler
/root/miniconda3/bin/python3 -m py_compile api_server.py collector_monitor.py device_control.py fleet_control.py monitor_scheduler.py cross_platform_monitor.py priority_station_scopes.py
systemctl restart amap-api
systemctl is-active amap-api
curl --fail --silent --show-error http://127.0.0.1:8800/health
curl --fail --silent --show-error -H 'X-Collector-Monitor-Key: 123456' http://127.0.0.1:8800/api/v1/collector-monitor
curl --silent --output /dev/null --write-out '%{http_code}\n' http://127.0.0.1:8800/api/v1/collector-monitor
```

最后一条未登录应返回 401；带密码的请求应为 200。若返回 503 / COLLECTOR_MONITOR_KEY_NOT_CONFIGURED，说明变量未进入服务环境；先修正配置，不切入口。确认返回的 ingestion 和 operations 均有数据，不能仅以 HTTP 200 判断数据库读取正常。新设备指令表和恢复遥测表由启动过程幂等创建，保留既有设备、租约、任务进度与历史结果。

## 4. 仅切 Nginx 网页入口

参考同目录 nginx-station-dashboard-locations.conf，把里面的专用 location 加到**当前实际生效的 station-dashboard server 块**。若已有同路径定义，只合并这些冲突项；不要整段替换 server。

- 精确根路径 / 跳转到 /station-dashboard。
- /station-dashboard 及其资源、/collector-monitor 别名转发至 127.0.0.1:8800。
- 精确 /api/v1/collector-monitor 及其子路径转发至 127.0.0.1:8800，包含单设备启停和全部停止。
- **原 location /api/ 转发至 127.0.0.1:8088 原样保留。** /api/health、/api/overview、/api/ai-report/daily、/api/ai-report/dates 等仍由旧 Go 服务处理，EVCS 原接口和数据库连接不变。
- 原 location / 的 SPA fallback 可以保留；新增的 location = / 优先匹配根路径。不要增加第二个相同的 location /。
- 保留域名、listen、证书、访问日志及其他已有路由。


```bash
nginx -t
systemctl reload nginx
```

只在 nginx -t 通过后 reload。全过程不执行 systemctl stop/restart station-dashboard，也不关闭或改动日报调度。

## 5. 手机升级与验收

逐台覆盖安装本次 APK，不卸载、不清数据。远程启停接收器依附无障碍服务：手机需要联网、无障碍已连接、App 进程存活，并完成设备登记。被系统强行停止后无法靠网页唤起 App，需先在手机上打开一次。

“停止”结束此客户端的采集循环并释放它的在途任务，不关闭接收远程指令的无障碍服务。未升级 App 的客户端显示“升级 App 后可远程启停”，不会把发出指令当成启动成功。手机继续使用原采集服务器地址，无需改为日报服务地址。

验收动作会真实控制设备，选一台百度与一台高德依次操作：

1. 旧网页入口进入新版，密码 123456 可登录，错误密码被拒绝。
2. 百度点开始，手机启动“设备调度”，不是本地队列或列表巡检；高德点开始，手机启动“重点站点详情采集”。
3. 只停止选中的客户端，其他客户端继续运行；同一实体手机中的百度、高德分行独立控制。等到“停止已执行”的真实回执，再点开始并等到启动回执。
4. 设备行展示版本、当前任务/运行状态、心跳年龄及北京时间、最近入库，以及近 10/30/60 分钟的快照数和折算速度。超过 90 秒无心跳标记超时；网页约 5 秒刷新，入库统计最多缓存 25 秒。
5. 2 小时趋势和整轮去重覆盖正常；DeepSeek 日志只展示手机实际上报的调用。没有新恢复事件时不会补造历史日志。
6. 全停功能会暂停全部派发，需要单独确认；此项按现场需要再做，不能拿它代替单台停止验收。
7. 再次检查原 station-dashboard 的 PID、启动时间、8088 健康与日报 dates GET，并通过旧域名请求 /api/health 和 /api/ai-report/dates，确认仍走原服务。保留已有日报及 EVCS 正常读取，勿调用生成接口。

设备速度按该客户端真实入库的正式站点快照折算为“站次/小时”，同一站重复采集会计入站次，不等于去重站点覆盖率。心跳在线仅说明客户端在上报，运行状态和最近入库用来判断是否实际产出。

## 6. 回退

只回退本次修改的 Nginx 专用路由即可恢复旧网页入口，先 nginx -t，再 reload。原 Go 后端始终运行，无需恢复或重建日报服务。若需回退采集 API，恢复采集文件备份后重启 amap-api；保留新增 SQLite 表、已有任务进度和入库数据，不删库、不重跑迁移。手机 App 可保留新版，新增远程功能在旧 API 上不会启用。

## 本机只读预览

运行 python preview_collector_monitor.py，用终端给出的本机地址查看。preview=1&demo=1 使用明确标识的演示数据。预览不注册控制接口，不能用来验证生产设备启停。

实体手机归属名单可在 MOBILE_DB_PATH 同目录的 collector-monitor-devices.json 中维护，沿用现有名单，勿在发布中覆盖；同机百度、高德按各自已核实的客户端 UUID 映射。诊断登记应列入 excluded。未核实归属的入库客户端仍显示来源和速度，但不会把历史记录伪装成可控设备。
