# K60 一键全部停止（2026-10-30）

百度 0.1.41（42）新增“一键全部停止（释放采集中任务）”按钮；高德升级至 0.1.42.12（54），接收同一套停止指令。固定百度499站、高德647站循环范围保持不变。

## 使用效果

1. K60点击按钮后，服务端先持久化停止指令，禁止所有设备领取新任务，再释放 MySQL 两类任务队列及 SQLite 队列中所有 LEASED/RUNNING 任务，包含设备遗留的其他租约。
2. 当前固定循环任务回到待领取，停止不消耗任务重试次数。历史失败补采任务回到失败补采队列。已完成任务及已入库数据保留；旧租约不能继续完成任务或上传。
3. 两端在采集中及任务间隙每3秒检查指令，收到后停止采集并取消冷却唤醒。正常联网时通常数秒生效，实际时间受网络请求影响；离线设备恢复联网后停止，无法承诺离线即时停止。
4. 停止状态跨服务端重启、手机服务重启持续有效。需在各设备手动点击开始恢复该设备，恢复一台不会恢复其他设备。按钮请求ID持久化，网络超时后重试同一请求不会重复发布停止指令。

## 部署顺序（必须更新服务端及全部三台手机）

本次功能基于主仓库 master 提交 74b7afd485065613976065566ce16571cecff0e7。服务端实现已直接纳入仓库根目录 api_server.py 和 fleet_control.py；从远程 master 拉取本次更新即可部署。

1. 备份当前部署源码及 MOBILE_DB_PATH 对应 SQLite 数据库（使用 SQLite 备份方式），保留现有 .env 和 MySQL。
2. 在服务端部署仓库核对本地改动后运行 `git pull --ff-only origin master`，确认更新包含根目录 fleet_control.py。Git更新已包含服务端改动，不要再次应用 server.patch；补丁仅供基于旧版本的离线部署，sources/server 提供对应完整文件用于核对。
3. 更新运行环境实际使用的 api_server.py，新增同目录 fleet_control.py。按原有流程部署 /opt/amap-crawler 并重启 systemd amap-api；无需清空任务，无需改 MySQL 表。启动会给原 SQLite 库自动创建3张控制表，并在同目录使用 .dispatch-lock 文件做多进程调度互斥；服务用户须有目录写权限。
4. 默认仅 BD- 设备代码且注册机型为 23013RK75C、Redmi K60 或 K60 的百度端有全停权限。可在服务环境中配置 MOBILE_FLEET_CONTROLLER_DEVICE_CODES 为 K60 的完整设备代码（多个用逗号分隔）以明确限制控制设备；此配置覆盖机型默认规则。其他手机只接收停止指令。
5. 保持现有配置覆盖安装 delivery/baidu-collector-0.1.41.apk 到 K60，delivery/amap-collector-0.1.42.12.apk 到两台高德手机。新客户端开始前要求服务器支持全停功能，故先部署服务器。
6. 只读检查 `/health` 与已认证的 GET /api/v1/devices/me/config，应有 fleetControl.supported=true；K60应有 canStopAll=true。勿为了验证部署主动调用 stop-all/resume 或启动实际采集。

## 接口

- POST /api/v1/devices/stop-all：设备Bearer认证，控制设备权限，body为 {"requestId":"唯一且重试不变的ID"}。
- GET /api/v1/devices/me/config：fleetControl 包含 stopGeneration、stopRequired、releaseComplete、canStopAll。
- POST /api/v1/devices/me/stopped：收到停止后回执，body为 {"stopGeneration":1}。
- POST /api/v1/devices/me/resume：仅手动开始时调用，body为当前 stopGeneration；旧版本号或释放未完成时拒绝恢复。
- 停止期间领取返回 task=null, reason=FLEET_STOPPED。

若数据库释放失败，停止屏障仍保持，界面会提示未确认；应从原K60按钮重试，让同一个requestId完成释放。释放计数以已提交的操作为准；极少数跨库提交后进程崩溃情形下重试计数可能低于实际释放量，但禁止领取和幂等释放仍生效。

## 交付与验证

server.patch、baidu.patch、amap.patch 及 sources/ 是本次改动，应用源码基线分别为百度0.1.40和高德0.1.42.11；不含密钥、.env、数据库或手机配置。manifest.json记录改动前后文件摘要及APK摘要。

Android实际构建源码位于独立工作目录；本仓库根目录 android/ 不是本次安装包的构建源。远程保留 baidu.patch、amap.patch 及 sources/ 用于复现本次客户端变更。APK在本地 delivery/ 下，不随此次代码提交上传。

两款APK assembleDebug成功；Python语法检查通过；原任务调度7项及全停8项离线检查通过。未启动实际采集，未对线上发送全停、释放或恢复指令。生成时 ADB 未检测到设备，尚未安装；服务端尚未部署。
