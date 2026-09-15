$ErrorActionPreference = "Stop"

$root = "D:\TigerCode\amap-charging-station-crawler"
Set-Location $root

# Load database configuration from .env.
$configPath = "D:\TigerCode\amap-charging-station-crawler\.env"
if (Test-Path $configPath) {
    Get-Content $configPath | ForEach-Object {
        if ($_ -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$') {
            [Environment]::SetEnvironmentVariable(
                $matches[1],
                $matches[2].Trim(),
                "Process"
            )
        }
    }
}

$python = Join-Path $env:LOCALAPPDATA "Programs\Python\Python312\python.exe"
if (-not (Test-Path $python)) {
    $python = "python.exe"
}

& $python -X utf8 (Join-Path $root "run_backend.py")
