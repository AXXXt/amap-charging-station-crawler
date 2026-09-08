# Android 无障碍采集终端

这是把电脑端 `crawler.py` 迁移到手机后的 Kotlin 工程，包含两个模块：

- `core`：纯 JVM 页面识别、详情解析、分时电价解析和去重规则，带 JUnit 测试。
- `app`：Android 无障碍服务、采集状态机、Room 本地任务/结果缓存、前台常驻服务和主界面。

## 构建

```powershell
$env:JAVA_HOME="C:\Users\12495\.cache\codex-runtimes\android-build\jdk17\jdk-17.0.16+8"
& "C:\Users\12495\.gradle\wrapper\dists\gradle-8.9-bin\90cnw93cvbtalezasaz0blq0a\gradle-8.9\bin\gradle.bat" -p android :core:test :app:assembleDebug --no-daemon
```

APK 输出在 `android\app\build\outputs\apk\debug\app-debug.apk`。

## 首次运行

1. 安装 APK 并打开应用。
2. 点击“打开无障碍设置”，在系统设置中开启“高德充电站采集助手”。
3. 填写服务端地址、设备激活码和设备名称，点击“保存配置”。
4. 点击“开始设备调度”让手机持续领取服务端任务；也可以使用“本机扫描”直接按城市/区县/关键词采集。

## 服务端

运行仓库根目录的 FastAPI 服务：

```powershell
.\run_api.ps1
```

服务默认监听 `http://0.0.0.0:8800`，已内置移动端控制面接口：

- `POST /api/v1/devices/register`
- `POST /api/v1/devices/heartbeat`
- `POST /api/v1/device-tasks/claim`
- `POST /api/v1/device-tasks/{taskId}/ack|progress|complete|fail`
- `POST /api/v1/observations/batches`

电脑重启会结束原来的 API 进程。建议在电脑端首次配置完成后执行项目根目录的 `install_backend_autostart.ps1`，让服务在 Windows 登录后自动恢复；服务端恢复前，手机端会保留待上传结果并继续重试。

首次启动会在 `data/mobile_control.db` 中生成默认河南省区县扫描任务。默认激活码为 `dev-activate`，可通过环境变量 `MOBILE_ACTIVATION_CODE` 修改。

## 合规与生产要求

- APK 不直连 MySQL，所有数据通过服务端 API 上报。
- 无障碍服务必须由用户在系统设置中主动开启。
- 开发调试可以使用 HTTP；生产部署必须改为 HTTPS，并更换默认激活码。
- 正式运行不依赖 ADB 或 uiautomator2。

## 实机监控注意事项

- 采集运行期间不要执行 `uiautomator dump`，也不要连接 `uiautomator2`。Android 注册 `UiAutomationService` 时会临时解绑普通无障碍服务，表现为采集助手反复出现“已解绑/已销毁”。
- 日志监控使用 `adb logcat -s EvCollector:V EvCollectorPage:V AndroidRuntime:E`。`EvCollectorPage` 是采集助手通过自身无障碍快照输出的高德页面摘要，不会占用额外的 `UiAutomation` 通道。
- 查看前台窗口使用 `adb shell dumpsys window`，查看画面使用 `adb exec-out screencap -p`；这两种方式不会干扰采集助手的无障碍连接。
