@echo off
rem 由 Yamtrack 本地安装脚本生成 —— Redis 看护进程
rem 说明：真正启动 Redis 的是 redis-daemon.py，它会在 Redis 被意外终止时自动重启。
rem       请勿关闭本窗口 —— 关掉它保护就失效了（Redis 本身以无窗口方式运行）。
rem       日志见 .runtime\logs\redis-daemon.log
chcp 936 >nul 2>&1
title Yamtrack Redis（请勿关闭）
cd /d "%~dp0.."
if not exist "%~dp0..\.runtime\logs" mkdir "%~dp0..\.runtime\logs"
"%~dp0..\.venv\Scripts\python.exe" "%~dp0redis-daemon.py" > "%~dp0..\.runtime\logs\redis-daemon-stdout.log" 2>&1
