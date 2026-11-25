# 2026-11-25 采集调度独立开轮部署交接

仓库：https://github.com/AXXXt/amap-charging-station-crawler

发布分支：codex/collector-independent-rounds-20261125。部署时核对交接消息中的完整提交号，并记录实际部署的提交号。

## 部署目标

更新既有采集调度/API 服务，使百度、高德在各自主任务一轮结束后，按各自配置进入下一轮；上一轮跨端补采继续在目标平台领取、上报和入库。消除的是等待对方补采造成的开轮阻塞；主任务仍在执行、设备轮休、设备离线或主动设置的轮间间隔仍按原规则处理，不能把所有“无待领取任务”都归因于同一种等待。

正式站点配置同步为百度 **1080**、高德 **138**，合计 **1218**。这是 2026-11-23 的 148 站试采中，86 站同时取得分时电价与有效电桩状态后已经完成的迁移。配置保留原 source_key 和 source_station_id，使用试采验证过的百度站名；本次上线不重复迁移数据库、不重置批次。

本次范围是采集服务端。手机沿用已安装版本。采集监控台另行部署，本次不切换旧 station-dashboard 入口，不更新 Nginx、EVCS 或监控台静态资源。

## 生产更新清单

既有记录：服务器目录 /opt/amap-crawler，systemd 服务 amap-api，直连端口 8800，既有 Nginx 反代端口 8082。如运维配置已迁移，以实际服务配置为准。

仅覆盖下面四个生产文件，必须成套更新：

- monitor_scheduler.py
- cross_platform_monitor.py
- priority_station_scopes.py
- config/priority_station_scopes.json

测试文件 test_cross_platform_monitor.py 和本文随提交提供，用于验证和交接。此次发布没有监控台改动，不需要替换 api_server.py、.env 或 data/。发布基于远程 dcf0eba，目标服务器应已具备跨端补采基础功能（api_server.py 调用 monitor_scheduler、fleet_control.py 与 station_monitor_supplement_task 表）；若仍是没有跨端补采的老服务，先核对 20261113 基础发布说明，不能只复制本次四个文件到缺少基础依赖的环境。

本次无新增依赖、无新增数据库结构变更。保留现有 .env、data/、设备登记、租约、任务进度和所有入库历史。更新配置文件不会自动改写现有数据库批次名单；上线前应只读核对正式批次仍是 1080/138，不能靠重建批次“修复”差异。

## 更新步骤

通过既有运维方式连接服务器，进入 Bash。以下假设项目目录是 Git 工作副本，并具有读取仓库权限。若服务器使用制品发布，请从同一提交导出下列四个文件后走现有发布流程，不需要把手机项目或监控台一起发布。

### 1. 拉取指定分支、准备四个文件并备份

~~~bash
set -euo pipefail
cd /opt/amap-crawler

git fetch origin refs/heads/codex/collector-independent-rounds-20261125:refs/remotes/origin/codex/collector-independent-rounds-20261125
RELEASE_COMMIT=$(git rev-parse refs/remotes/origin/codex/collector-independent-rounds-20261125)
git show --no-patch --format=fuller "$RELEASE_COMMIT"
# 在继续前核对 RELEASE_COMMIT 与交接消息一致。

STAGE=$(mktemp -d /tmp/collector-rounds-20261125.XXXXXX)
git archive "$RELEASE_COMMIT" \
  monitor_scheduler.py cross_platform_monitor.py \
  priority_station_scopes.py config/priority_station_scopes.json \
  | tar -x -C "$STAGE"

BACKUP="/opt/amap-crawler/backups/collector-rounds-$(date +%Y%m%d-%H%M%S)"
mkdir -p "$BACKUP"
cp -a --parents monitor_scheduler.py cross_platform_monitor.py \
  priority_station_scopes.py config/priority_station_scopes.json "$BACKUP/"
printf '%s\n' "$RELEASE_COMMIT" > "$BACKUP/release-commit.txt"
(cd "$STAGE" && sha256sum monitor_scheduler.py cross_platform_monitor.py \
  priority_station_scopes.py config/priority_station_scopes.json) \
  > "$BACKUP/release-files.sha256"
printf '备份目录：%s\n' "$BACKUP"
~~~

保留该终端中的 STAGE、BACKUP 和 RELEASE_COMMIT 变量。既有运维记录中服务使用 /root/miniconda3/bin/python3；下一步解释器应与当前 amap-api 的实际 ExecStart 一致。

### 2. 发布前检查，再短暂停服切换

~~~bash
systemctl show amap-api --property=ExecStart --value
PYTHON_BIN=/root/miniconda3/bin/python3
"$PYTHON_BIN" -m py_compile "$STAGE/monitor_scheduler.py" \
  "$STAGE/cross_platform_monitor.py" "$STAGE/priority_station_scopes.py"
(cd "$STAGE" && "$PYTHON_BIN" -c \
  'import priority_station_scopes as p; print(p.scope_counts())')
# 预期 BAIDU_MIN240_499=1080，AMAP_MIN240_647=138。
# 同时按下文只读 SQL 核对数据库里的正式名单数量。

sudo systemctl stop amap-api
cp -a "$STAGE/monitor_scheduler.py" "$STAGE/cross_platform_monitor.py" \
  "$STAGE/priority_station_scopes.py" /opt/amap-crawler/
cp -a "$STAGE/config/priority_station_scopes.json" /opt/amap-crawler/config/
sha256sum -c "$BACKUP/release-files.sha256"
sudo systemctl start amap-api
sudo systemctl status amap-api --no-pager
curl --fail --silent --show-error --max-time 10 http://127.0.0.1:8800/health
curl --fail --silent --show-error --max-time 10 http://127.0.0.1:8082/health
sudo journalctl -u amap-api -n 80 --no-pager
~~~

停止服务到重新启动之间会短暂不可用。不能在名单校验失败或文件缺失时继续覆盖；若启动失败，按下文回退调度文件并核查日志。以上流程按提交提取四个文件，不要求清理或重置服务器工作区。

## 只读核查与自然采集验收

通过现有只读数据库连接执行，不在命令行输出连接口令。

正式批次和名单数量：

~~~sql
SELECT b.id,b.scope_key,b.status,b.station_count,
       COUNT(bs.id) AS member_count,b.current_round
FROM station_monitor_batch b
LEFT JOIN station_monitor_batch_station bs ON bs.batch_id=b.id
WHERE b.status='ACTIVE'
  AND b.scope_key IN ('BAIDU_MIN240_499','AMAP_MIN240_647')
GROUP BY b.id,b.scope_key,b.status,b.station_count,b.current_round;
~~~

每个平台应只有一个正式活动批次，百度 station_count/member_count 为 1080，高德为 138。若实际不符，保留当前批次与进度，先查明差异。

记录部署前后的当前轮号和主任务状态，再观察自然运行：

~~~sql
SELECT b.scope_key,b.current_round,r.status AS round_status,
       t.status AS task_status,COUNT(t.id) AS task_count
FROM station_monitor_batch b
JOIN station_monitor_round r ON r.batch_id=b.id AND r.round_no=b.current_round
LEFT JOIN station_monitor_task t ON t.round_id=r.id
WHERE b.status='ACTIVE'
  AND b.scope_key IN ('BAIDU_MIN240_499','AMAP_MIN240_647')
GROUP BY b.scope_key,b.current_round,r.status,t.status;

SELECT b.scope_key AS origin_scope,r.round_no AS origin_round,
       b.current_round,s.target_scope_key,s.status,COUNT(*) AS task_count
FROM station_monitor_supplement_task s
JOIN station_monitor_batch b ON b.id=s.origin_batch_id
JOIN station_monitor_round r ON r.id=s.origin_round_id AND r.batch_id=b.id
WHERE b.status='ACTIVE' AND s.status IN (0,1,2)
GROUP BY b.scope_key,r.round_no,b.current_round,s.target_scope_key,s.status;
~~~

补采状态：0 待领取、1 已领取、2 运行中、3 完成、4 失败。自然运行验收条件：

1. 某平台本轮主任务全部结束，即使还有待领取或执行中的跨端补采，也可生成该平台下一轮主任务；无需等待对方主任务结束。固定正式队列原配置轮间间隔为零。
2. 另一平台随后仍能领取 origin_round 小于 current_round 的旧轮 XPL- 补采，ack/progress/complete 正常，对应任务的新快照可以入库。补采仍优先于该设备的新主任务，每个补采仍只尝试一次。
3. 数据库正式名单维持 1080/138，进度和历史记录持续累积。旧监控台入口照常使用，不能用页面未更新判断本次后端补丁无效。

有限轮次试验批次在最后一轮结束时仍等待本批次各轮补采收尾；这与正式无限循环队列的独立开轮不同。未领取补采仍可等待目标平台上线；已领取补采维持原 15 分钟租约与续约规则。主任务开轮不再因此阻塞，不代表离线设备已经执行了补采。

不要通过主动领取生产任务、重置批次、切换名单或清空补采来验收。健康接口 200 证明服务可达，实际消除等待和速度变化应以自然采集的轮号、领取日志与入库记录为准。

## 本地验证

发布目录隔离于本机预览工作区，未复制 .env 或本机数据库。

- python -m unittest test_cross_platform_monitor test_monitor_retry_limit test_mobile_dispatch test_fleet_control test_daily_tariff_policy test_site_exploration_bridge -q：83 项通过。
- 覆盖双向独立开轮、旧轮待领取/执行中补采继续、父批次暂停/恢复、有限批次最终收尾、租约过期和全停控制。
- 固定名单通过加载校验：1080+138=1218，无重复站点身份；原百度站点未减少，86 个迁移站点的原始标识均保留。
- Python 语法检查、git diff --check 通过。本地验证不等于已部署或已实测提速。

## 回退

记录上文输出的备份路径。若需要回退本次开轮逻辑，成套恢复部署前的两个调度模块，保留 1080/138 正式名单配置和数据库进度。priority_station_scopes.py 与名单文件已经成对备份；如要同时回退名单配置，必须先确认与数据库正式名单一致，不能直接恢复为旧的 994/224。

~~~bash
cd /opt/amap-crawler
# 如已更换终端，先把 BACKUP 设置为本次发布记录的实际备份目录。
test -f "$BACKUP/monitor_scheduler.py"
test -f "$BACKUP/cross_platform_monitor.py"
sudo systemctl stop amap-api
cp -a "$BACKUP/monitor_scheduler.py" "$BACKUP/cross_platform_monitor.py" /opt/amap-crawler/
sudo systemctl start amap-api
curl --fail --silent --show-error --max-time 10 http://127.0.0.1:8800/health
sudo journalctl -u amap-api -n 80 --no-pager
~~~

回退会恢复旧版等待补采后才开新轮的行为；旧版也无法继续领取已完成旧轮的待领取补采。已经开出的新轮、旧轮补采和历史结果保留，不回写轮号、不删表。重新上线前需核对残留补采状态。

## 可直接转发给部署同事

请先部署采集调度后端这版“百度/高德独立开轮”补丁。仓库 AXXXt/amap-charging-station-crawler，分支 codex/collector-independent-rounds-20261125，以交接消息里的提交号为准。更新 /opt/amap-crawler 的 monitor_scheduler.py、cross_platform_monitor.py、priority_station_scopes.py、config/priority_station_scopes.json，重启 amap-api；完整命令、验收和回退见本文。目标是某个平台主任务结束后直接按自己的配置开下一轮，上一轮跨端补采继续执行。正式名单维持百度1080/高德138，保留 .env、data、任务进度和历史数据，不重建批次。重启后查8800/8082健康接口和日志，再按自然采集确认独立开轮与旧轮补采。手机沿用当前已安装版本。采集监控台、旧 station-dashboard 和 Nginx 这次先不动，等采集调度部署验收后再安排。
