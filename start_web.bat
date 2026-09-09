@echo off
rem ============================================================
rem 启动 Web 服务 + 选股调度 (app.py)  端口 7865
rem ============================================================
cd /d D:\CASE-AI量化系统
set PYTHONUTF8=1
title AI量化系统 - Web+选股调度(app.py)
.venv\Scripts\python.exe -X utf8 app.py
pause