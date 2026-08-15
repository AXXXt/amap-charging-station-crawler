# 充电站采集一键启动脚本
# 用法: .\run_crawl.ps1 [--city 郑州] [--districts 中原区,金水区] [--visual] [--import-db] [--output xx.json]
$ErrorActionPreference = "Stop"
cd $PSScriptRoot

# 设备与接口配置
$env:ADB_PATH = "D:\TigerCode\platform-tools\platform-tools\adb.exe"
$env:DEVICE_SERIAL = "63c48adb"
$env:AMAP_API_KEY = "90e15fd35dc6f5832938d51a2f10789c"
$env:DASHSCOPE_API_KEY = "sk-ws-H.EERMDYY.Wvhw.MEUCIQCHsjU0WFfX1Rdl0gap6ZOcsdg6y60EfNjA3zy1pkUUaAIgX6_YzL8IG_gL67pSGW8prjDf5HuhBoUsedMGFJmynr0"

# MySQL 数据库配置
$env:DB_HOST = "121.41.56.201"
$env:DB_PORT = "3306"
$env:DB_USER = "anxitong_u"
$env:DB_PASSWORD = "1d0Pb8s21d0PbLGx78Pdqqc6"
$env:DB_NAME = "evcs"
$env:DB_CHARSET = "utf8mb4"

$py = "C:\Users\12495\AppData\Local\Programs\Python\Python312\python.exe"
& $py run_crawl.py @args
