# 启动 API 服务（带 MySQL 配置）
# 用法: .\run_api.ps1
$ErrorActionPreference = "Stop"
cd $PSScriptRoot

$env:DB_HOST = "127.0.0.1"
$env:DB_PORT = "3306"
$env:DB_USER = "root"
$env:DB_PASSWORD = "123456"
$env:DB_NAME = "evcs"
$env:DB_CHARSET = "utf8mb4"

$py = "C:\Users\12495\AppData\Local\Programs\Python\Python312\python.exe"
& $py api_server.py
