@echo off
setlocal EnableExtensions DisableDelayedExpansion
cd /d "%~dp0"

if not exist "logs" mkdir "logs"
if not exist "data" mkdir "data"

set "PYTHON=python"
%PYTHON% --version >nul 2>&1
if errorlevel 1 set "PYTHON=py -3"
%PYTHON% --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python was not found in PATH.
    pause
    exit /b 1
)

echo ============================================
echo   Heavy Truck Charging Station Service
echo ============================================
echo.
set "SERVER_STARTED=0"
%PYTHON% -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8800/api/tasks/status', timeout=3)" >nul 2>&1
if not errorlevel 1 goto ready

echo [1/3] Starting backend on port 8800...
start "Heavy Truck Charging Station Backend" /B %PYTHON% run_backend.py
set "SERVER_STARTED=1"

echo [2/3] Waiting for backend health check...
set /a ATTEMPTS=0
:wait_loop
set /a ATTEMPTS+=1
%PYTHON% -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8800/api/tasks/status', timeout=3)" >nul 2>&1
if not errorlevel 1 goto ready
if %ATTEMPTS% GEQ 30 (
    echo [ERROR] Backend did not become ready within 30 seconds.
    if exist "logs\api_server_error.log" type "logs\api_server_error.log"
    %PYTHON% stop_backend.py >nul 2>&1
    pause
    exit /b 1
)
timeout /t 1 /nobreak >nul
goto wait_loop

:ready
echo [3/3] Opening dashboard...
start "" "http://127.0.0.1:8800/dashboard"
echo.
echo Service is ready: http://127.0.0.1:8800/dashboard
echo API docs: http://127.0.0.1:8800/docs
echo.
echo Press any key to stop the backend.
pause >nul

if "%SERVER_STARTED%"=="1" %PYTHON% stop_backend.py >nul 2>&1
echo Backend stopped.
endlocal
exit /b 0
