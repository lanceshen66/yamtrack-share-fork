@echo off
rem 由 Yamtrack 本地安装脚本生成 —— Web 主服务
chcp 936 >nul 2>&1
title Yamtrack 主服务
cd /d "%~dp0..\src"
"%~dp0..\.venv\Scripts\python.exe" manage.py runserver 127.0.0.1:8000
