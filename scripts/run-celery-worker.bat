@echo off
rem 由 Yamtrack 本地安装脚本生成 —— Celery 任务执行器
chcp 936 >nul 2>&1
title Yamtrack Celery Worker
cd /d "%~dp0..\src"
if not exist "%~dp0..\.runtime\logs" mkdir "%~dp0..\.runtime\logs"
"%~dp0..\.venv\Scripts\python.exe" -m celery -A config worker --loglevel info --pool solo --without-mingle --without-gossip > "%~dp0..\.runtime\logs\celery-worker.log" 2>&1
