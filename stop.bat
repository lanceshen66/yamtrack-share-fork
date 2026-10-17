@echo off
rem 由 Yamtrack 本地安装脚本生成 —— 一键停止
chcp 936 >nul 2>&1
title Yamtrack 停止
cd /d "%~dp0"
setlocal

echo ==============================================
echo             Yamtrack 本地停止
echo ==============================================
echo.

echo [1/3] 正在停止 Web 服务 ...

rem 结束占用 8000 端口的进程
for /f "tokens=5" %%a in ('netstat -aon ^| findstr ":8000 " ^| findstr "LISTENING"') do (
    taskkill /F /PID %%a >nul 2>&1
)

rem 兜底：只结束本项目 .venv 里的 Django 进程
powershell -NoProfile -Command "Get-CimInstance Win32_Process | Where-Object { $_.ExecutablePath -like '*Yamtrack*.venv*' -and $_.CommandLine -like '*manage.py*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }" >nul 2>&1

echo [2/3] 正在停止 Celery ...
powershell -NoProfile -Command "Get-CimInstance Win32_Process | Where-Object { $_.ExecutablePath -like '*Yamtrack*.venv*' -and $_.CommandLine -like '*celery*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }" >nul 2>&1

echo [3/3] 正在停止 Redis ...
rem 先结束看护进程，再结束 Redis 本体
powershell -NoProfile -Command "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '*redis-daemon.py*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }" >nul 2>&1
taskkill /F /IM redis-server.exe >nul 2>&1

echo.
echo 已全部停止。
echo.

endlocal
timeout /t 4 /nobreak >nul
