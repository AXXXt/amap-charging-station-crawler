# 2026-11-04 采集调度重试上限更新

本次发布适用于高德、百度固定站点循环采集的 station_monitor_task 调度链路。
每站每轮最多执行 2 次，即首次采集加最多 1 次重试。新轮次重新计数。

## 部署步骤

1. 从 AXXXt/amap-charging-station-crawler 仓库更新 master，核对并保留服务端本地配置及 .env。若服务器运行目录不是 Git 仓库，从更新后的部署工作副本同步文件。
2. 本次服务端更新文件是 monitor_scheduler.py、priority_station_scopes.py、config/priority_station_scopes.json。配置文件和校验代码必须一起更新。沿用现有 api_server.py、fleet_control.py 及其他运行依赖，不使用 releases 中旧版 sources/server 覆盖当前代码。
3. 按现有部署流程备份并替换运行文件，然后重启采集调度 API 服务。既有部署记录为 /opt/amap-crawler、systemd 服务 amap-api，实际路径和服务名以线上为准。此服务与 EVCS 平台后端分开，仅重启 EVCS 后端不会应用本次改动。
4. 启动时自动将活动批次当前轮次未完成任务的 max_attempts 上限降至 2。已达到上限且处于 PENDING 的任务转为 FAILED 并同步轮次失败计数；LEASED/RUNNING 的任务保留当前租约，可正常上报。本次不会清零 attempt、重建批次或清空采集结果。
5. 检查启动日志、/health 和数据库中的上限；无需额外手工 SQL 迁移，也无需更新手机 App。不要通过全停、重置任务或启动实采来验证部署。

## 配置同步

当前固定名单为百度 499、高德 644，共 1143 站。高德历史 scope_key AMAP_MIN240_647 保留兼容性。
本次同步已在线上处理的 3 条通用名任务删除及已核准站名更新，避免后续建批次时恢复旧名单。
配置版本为 20261104-min240-v2。固定配置不会自动重命名已有批次任务；相关线上修改此前已完成。

## 只读验收

```sql
SELECT b.id AS batch_id, b.scope_key, b.current_round, t.status,
       t.max_attempts, COUNT(*) AS task_count
FROM station_monitor_task t
JOIN station_monitor_batch b ON b.id=t.batch_id AND b.current_round=t.round_no
WHERE b.status='ACTIVE' AND t.status IN ('PENDING','LEASED','RUNNING')
GROUP BY b.id,b.scope_key,b.current_round,t.status,t.max_attempts;
```

未完成任务不应存在 max_attempts > 2。若部署时已有第 3 次采集正在执行，attempt 可暂时为 3，保留其租约让当前执行收尾；之后不再追加重试。
NO_MATCH 耗尽重试仍沿用原有跳过语义（COMPLETED 且 result_summary.skipReason=NO_MATCH），不计作有效入库；错误失败才进入 FAILED。
