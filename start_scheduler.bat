@echo off
rem ============================================================
rem 启动定时任务 (scheduler.py)
rem   20:00 日线增量 / 15:35 分钟线 / 15:00 换手率
rem ============================================================
cd /d D:\CASE-AI量化系统
set PYTHONUTF8=1
title AI量化系统 - 定时任务(scheduler.py)
.venv\Scripts\python.exe -X utf8 scheduler.py
pause