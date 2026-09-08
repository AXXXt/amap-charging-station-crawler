# Start the PC-side API service. Suitable for logon startup or manual use.
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

if (-not (Test-Path (Join-Path $root "logs"))) {
    New-Item -ItemType Directory -Path (Join-Path $root "logs") | Out-Null
}
if (-not (Test-Path (Join-Path $root "data"))) {
    New-Item -ItemType Directory -Path (Join-Path $root "data") | Out-Null
}

$healthUrl = "http://127.0.0.1:8800/health"

function Test-Backend {
    try {
        $response = Invoke-WebRequest -Uri $healthUrl -UseBasicParsing -TimeoutSec 3
        return $response.StatusCode -ge 200 -and $response.StatusCode -lt 500
    } catch {
        return $false
    }
}

# Idempotent: repeated executions will not start multiple API processes.
if (Test-Backend) {
    exit 0
}

$pythonCommand = Get-Command python.exe -ErrorAction SilentlyContinue
if ($null -eq $pythonCommand) {
    $pythonCommand = Get-Command py.exe -ErrorAction SilentlyContinue
}
if ($null -eq $pythonCommand) {
    throw "Python was not found. Install Python 3.10+ and add it to PATH."
}

$arguments = @("-X", "utf8", "run_backend.py")
if ($pythonCommand.Name -ieq "py.exe") {
    $arguments = @("-3", "-X", "utf8", "run_backend.py")
}

Start-Process -FilePath $pythonCommand.Source -ArgumentList $arguments -WorkingDirectory $root -WindowStyle Hidden | Out-Null

# Allow time for Uvicorn and MySQL initialization. Return a nonzero exit code
# so scheduled-start failures are visible.
for ($attempt = 1; $attempt -le 30; $attempt++) {
    Start-Sleep -Seconds 1
    if (Test-Backend) {
        exit 0
    }
}

throw "API failed to start. See logs\api_server_error.log."
