# 高德 / 百度任务分流服务端部署说明（2026-10-27）

## 部署目的与代码来源

百度客户端已发送 `mode=BAIDU`，但旧服务端在 MySQL 中领取任务时没有使用 `collector` 过滤，导致百度手机会领取归属为 `AMAP` 的任务。这次修复服务端领取逻辑，需要部署并重启服务后才生效。

- 仓库：`AXXXt/amap-charging-station-crawler`
- 修复分支：`codex/collector-routing-fix`
- 基于远程 `master` 的 `c3d80549774a2e9057e5d19cb9e2b2d56c7f2129`，保留该版本的 observations 入库唯一索引冲突修复。
- 运行代码只需部署 `api_server.py`；本文用于交接。
- 高德、百度采集 App 均无需为本次修复重新安装。

## 修复行为

1. `BAIDU` 模式只领取 `collector=BAIDU` 的任务；默认 / `NORMAL` 模式按 `collector=AMAP` 领取。
2. 当前任务续领、待领取任务选择、失败重试选择及租约更新均限定相同归属；两类队列的待采 / 运行中任务判断互不阻塞。
3. `MONITOR` 仍走现有监控调度器，高德既有 1566 站监控批次继续使用现有配置。
4. 百度队列为空时返回 `QUEUE_EMPTY`。关闭 MySQL 权威源时，百度请求返回 503，避免落入没有归属过滤的旧 SQLite 队列。
5. 新库建表包含 `collector`；旧库缺少该列时自动补列，默认 `AMAP`。当前线上已有此列和分配结果，保留原有值；本修复不重新分配 1401 个百度任务。缺少列的新环境仍需另外配置站点分配，不能仅靠默认值建立百度队列。
6. 本次变更不新增自动循环、不改变完成 / 失败判定、不重置任务状态，也不修改历史结果来源。

## 部署位置与配置

| 项目 | 值 |
| --- | --- |
| 主机 | `116.62.103.230` |
| 目录 | `/opt/amap-crawler` |
| systemd 服务 | `amap-api` |
| API 端口 | `8800` |
| Python | `/root/miniconda3/bin/python3` |
| 现有启动方式 | `uvicorn api_server:app --host 0.0.0.0 --port 8800 --workers 2` |
| 权威源 | `HENAN_MYSQL_AUTHORITATIVE=1` |
| 领取模式 | `MOBILE_CLAIM_MODE=HENAN_ONLY` |

保留服务器 `.env`、`data/mobile_control.db`、MySQL 数据和现有监控批次。不要上传开发机数据库或重新运行任务分配 / 导入 / 重置脚本。

现有部署记录说明 `/opt/amap-crawler` **不是 Git 仓库**，代码通过 SCP 上传。请从修复分支取文件，不能仅在服务器目录执行 `git pull master`。

## 操作步骤

在同事拥有 Git 和 SSH 权限的工作机、已有仓库根目录执行。以下为 shell 命令；使用独立目录避免混入工作机未提交改动。

```bash
git fetch origin
git worktree add --detach ../amap-collector-routing-deploy origin/codex/collector-routing-fix
git -C ../amap-collector-routing-deploy rev-parse HEAD
scp ../amap-collector-routing-deploy/api_server.py root@116.62.103.230:/opt/amap-crawler/api_server.py.collector-routing-new
```

记录上一步的提交号，和交接消息中的提交号核对。若该部署目录已经存在，请使用另一个空目录名。

登录服务器后执行以下步骤，先备份原代码，再替换和重启全部 worker：

```bash
set -eu
cd /opt/amap-crawler
deploy_backup="api_server.py.bak-collector-routing-$(date +%Y%m%d-%H%M%S)"
cp -p api_server.py "$deploy_backup"
chmod --reference=api_server.py api_server.py.collector-routing-new
chown --reference=api_server.py api_server.py.collector-routing-new
mv -f api_server.py.collector-routing-new api_server.py
systemctl restart amap-api
systemctl is-active amap-api
systemctl show amap-api -p ExecMainStartTimestamp --no-pager
journalctl -u amap-api -n 60 --no-pager
curl -fsS http://127.0.0.1:8800/health
sha256sum api_server.py
printf '代码备份：%s\n' "$deploy_backup"
```

用工作机文件的 SHA-256 与服务器输出核对（Windows 可用 `Get-FileHash -Algorithm SHA256`，Linux 可用 `sha256sum`）。只替换文件而未重启，会继续运行内存中的旧逻辑。

以上服务状态 / 健康检查用于确认部署生效；本次交付没有执行采集测试，也不要求手动调用领取接口做测试。`/health` 返回成功只能说明进程存活，不能单独证明分流正确。

## 部署后的观察口径

可只读观察自然产生的新租约：百度设备 `BD-49C0F89A`（设备 ID `72925aab-1244-404d-aaff-56cd98e3c4e5`）部署后新领取的普通任务应为 `BAIDU`；高德继续使用现有监控批次。若查看原始 MySQL 任务时间，按 UTC 解读，展示时转换为北京时间。

旧版本已发出的租约可能在部署后继续回传完成结果，这是现有在途任务。本修复不撤销它们。更早的 `BAIDU_DETAIL` 结果关联 `AMAP` 任务也仍会保留，不要为修正显示批量重写 `collector`。任务归属和实际采集来源分别查看任务表 `collector` 与结果 `result_payload.sourcePage`。

由于旧版本曾混领任务，原始“1401 个”是分配规模，不是此次部署后的待采数量；本修复不重新计算或重置剩余任务。

## 回滚

若部署后服务异常，先保留日志；用本次实际记录的备份文件替换 `api_server.py` 并重启 `amap-api`。无需删除 `collector` 列。回滚会恢复旧的混领风险，需告知采集负责人。

## 交付检查范围

仅审阅代码差异、Python 语法和 Git 空白格式；未启动服务或采集器，未调用领取接口，未修改线上数据库，未进行采集 / 接口联调测试。生产部署由同事执行。
