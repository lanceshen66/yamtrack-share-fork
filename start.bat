@echo off
rem 由 Yamtrack 本地安装脚本生成 —— 一键启动
chcp 936 >nul 2>&1
title Yamtrack 启动器
cd /d "%~dp0"
setlocal

echo ==============================================
echo             Yamtrack 本地启动器
echo ==============================================
echo.

rem ========== 步骤 1/4：Redis（必须最先确保在位）==========
rem 先看 Redis 看护进程在不在：0 或取不到值 = 需要拉起
set "DAEMON_COUNT=0"
for /f "usebackq delims=" %%a in (`powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\check-daemon.ps1"`) do set "DAEMON_COUNT=%%a"

if "%DAEMON_COUNT%"=="0" goto start_daemon
if "%DAEMON_COUNT%"=="" goto start_daemon

rem 看护进程已在运行，确认 Redis 本体也活着（可能正处在自动重启的间隙）
".runtime\redis\redis-cli.exe" -p 6379 ping >nul 2>&1
if not errorlevel 1 goto redis_ready
echo [1/4] Redis 掉线了，等待看护进程自动重启 ...
goto wait_redis_start

:start_daemon
echo [1/4] 正在启动 Redis 看护进程 ...
start "Yamtrack Redis（请勿关闭）" /min "%~dp0scripts\run-redis.bat"

:wait_redis_start
set /a tries=0
:wait_redis
set /a tries+=1
timeout /t 1 /nobreak >nul
".runtime\redis\redis-cli.exe" -p 6379 ping >nul 2>&1
if not errorlevel 1 goto redis_ready
if %tries% lss 20 goto wait_redis

echo.
echo      [x] Redis 启动失败。
echo          详情请看日志： .runtime\logs\redis.log
echo          看护进程日志： .runtime\logs\redis-daemon.log
echo          常见原因：6379 端口被其他程序占用。
echo.
goto :done

:redis_ready
echo [1/4] Redis 就绪

rem ========== 已有实例检测（放在 Redis 之后，可顺带修好"服务在跑但 Redis 掉了"的情况）==========
netstat -aon | findstr ":8000 " | findstr "LISTENING" >nul 2>&1
if not errorlevel 1 (
    echo [2/4] 检测到 Web 服务已在运行，跳过重复启动。
    echo.
    start "" http://localhost:8000
    goto :done
)

rem ========== 步骤 2/4：Celery 任务执行器 ==========
echo [2/4] 正在启动 Celery 任务执行器 ...
start "Yamtrack Celery Worker（请勿关闭）" /min "%~dp0scripts\run-celery-worker.bat"

rem ========== 步骤 3/4：Celery 定时调度器 ==========
echo [3/4] 正在启动 Celery 定时调度器 ...
start "Yamtrack Celery Beat（请勿关闭）" /min "%~dp0scripts\run-celery-beat.bat"

rem ========== 步骤 4/4：Web 服务 ==========
echo [4/4] 正在启动 Web 服务 ...
start "Yamtrack 主服务（请勿关闭）" "%~dp0scripts\run-web.bat"

timeout /t 6 /nobreak >nul

rem ========== 启动后复检：确认 Redis 在这几秒里没有掉线 ==========
".runtime\redis\redis-cli.exe" -p 6379 ping >nul 2>&1
if errorlevel 1 (
    echo.
    echo      [!] 警告：Redis 刚刚掉线了。
    echo          看护进程会自动重启它；若反复失败请看 .runtime\logs\redis-daemon.log
    echo.
)

start "" http://localhost:8000

echo.
echo 启动完成，访问地址： http://localhost:8000
echo 首次使用请先点击 "Sign Up" 注册一个账号。
echo.
echo 停止服务： 运行 stop.bat
echo 出问题时： 日志都在 .runtime\logs\ 目录下
echo 注意：      标题带"请勿关闭"的后台窗口请保持运行
echo.

:done
echo 本窗口 10 秒后自动关闭...
timeout /t 10 /nobreak >nul
endlocal
