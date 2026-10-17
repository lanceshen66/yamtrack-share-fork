@echo off
rem 由 Yamtrack 本地安装脚本生成 —— Celery 定时调度器
rem 注意：Windows 下 beat 必须与 worker 分开运行，不能用 worker -B
chcp 936 >nul 2>&1
title Yamtrack Celery Beat
cd /d "%~dp0..\src"
if not exist "%~dp0..\.runtime\logs" mkdir "%~dp0..\.runtime\logs"
"%~dp0..\.venv\Scripts\python.exe" -m celery -A config beat --loglevel info > "%~dp0..\.runtime\logs\celery-beat.log" 2>&1
