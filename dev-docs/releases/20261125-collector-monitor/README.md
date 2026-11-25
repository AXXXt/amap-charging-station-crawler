# 2026-11-25 采集监控台设备控制发布

此发布将设备改为可逐台操作的列表，百度开始“设备调度”，高德开始“重点站点详情采集”；展示客户端版本、运行状态、任务、心跳、近 10/30/60 分钟速度和指令执行回执。监控登录密码为 123456，通过 amap-api 的 COLLECTOR_MONITOR_KEY 配置。

网页、采集 API、手机 App 需要配套更新。旧 station-dashboard 的 Go 后端与日报/EVCS 服务始终保持原样运行，本次只替换其网页入口。

## 源码与版本

| 组件 | 分支 / 版本 |
| --- | --- |
| AXXXt/amap-charging-station-crawler | codex/collector-monitor-device-control-20261125 |
| 高德 App | 0.1.42.28，versionCode 70，com.tigercode.evcollector |
| hjw-alt/collector-baidu | codex/baidu-monitor-control-20261125 |
| 百度 App | 0.1.75，versionCode 76，com.tigercode.bdcollector |

百度固定 commit：890a78db6bf0165b77b5e9bbaa404dabce63f4df。

交接消息和发布包 RELEASE.txt 提供本次两个完整 commit 和 SHA256SUMS。按固定 commit 发布。百度包含原已安装的 0.1.72–0.1.74 恢复/轮休修复，不回退到旧 main。

## 操作说明

详细步骤见 [collector-monitor-cutover.md](../../collector-monitor-cutover.md)，合并到既有 server 的路由见 [nginx-station-dashboard-locations.conf](../../nginx-station-dashboard-locations.conf)。生产采集目录历史上是 scp 发布目录，文档提供固定 commit 的显式文件清单打包方式。

本次包含上一版已推送的独立开轮逻辑，名单保持百度 1080、高德 138，合计 1218。不要重复执行之前的迁移脚本或清理批次。

## 设备控制的行为

- 一行对应一个采集 App 客户端；同机的两个 App 可分别启停。
- STOP 只释放选中客户端的任务租约，停止其采集循环；无障碍接收器仍可接收 START。
- START 设置平台正确模式，等待旧引擎释放完、再启动采集前台服务，启动后回执；服务端指令持久化且按 requestId 幂等。
- 新指令和全停覆盖旧指令；丢失回执只补发，不重复启动。
- 手机离线时显示等待接收；旧 App 禁用远程按钮；无障碍关闭或 App 被强行停止需现场恢复。
- 浏览器显示“指令已发送”与“已执行”两种状态，失败显示原因，不把请求成功当作手机已启动。
- 手机心跳约 15 秒，超过 90 秒标记超时；控制轮询约 3 秒，界面约 5 秒刷新，入库缓存最多 25 秒。
- 速度来自正式站点真实入库快照，折算单位站次/小时；整轮覆盖按站点去重，两个指标分开解释。
- 登录密码仅驻留浏览器内存，退出会清除；服务器鉴权覆盖监控读取和控制接口。

## 验证记录

- 后端：105 项回归通过，包括设备控制、整轮覆盖、全停、跨端补采、补采上限、任务领取、日电价缓存与入库桥接。
- 两端 App：assembleDebug、testDebugUnitTest、core:test 均通过；高德 146 项、百度 197 项单元测试通过，每端包含 9 项指令处理测试。
- 浏览器：14 项隔离交互检查通过，包括密码、平台模式、逐台启停、真实回执、幂等重试、全停确认、旧 App/只读限制、故障清理，以及 1440/768/390 像素布局与运行错误检查。
- 截图与演示由本机隔离接口提供，没有借验收向生产手机发送控制指令。
- 尚未部署生产，也未安装到现场手机。Linux Nginx 配置必须由部署人员在实际配置合并后执行 nginx -t。

## Android 重新构建

使用 JDK 17、Android SDK 35、Gradle 8.9，分别在高德 android/ 和百度仓库根目录执行：

```bash
./gradlew :app:assembleDebug :app:testDebugUnitTest :core:test
```

高德输出 android/app/build/outputs/apk/debug/app-debug.apk；百度输出 app/build/outputs/apk/debug/app-debug.apk。本次交付包已附编译好的两个 APK，覆盖升级时保留现有配置和设备登记。

## 给部署同事的核心要求

发布采集 API、监控网页和两个 App；在采集服务设置 COLLECTOR_MONITOR_KEY=123456；只重启 amap-api。在旧 station-dashboard 的 Nginx 中只增加新版页面与监控 API 路由，保留原 /api/ 到 8088 的反代。不要停止/重启 station-dashboard，不动其二进制、.env、日报调度、1146 站日报范围和 EVCS 数据库/接口。先备份、检查，再 nginx -t/reload；最后确认旧日报服务 PID 和只读接口保持正常。
