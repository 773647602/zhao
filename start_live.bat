@echo off
rem ============================================================
rem 启动真实交易循环 (live_loop.py)  TRADER_DRY_RUN=0 = 真实下单
rem   :. 监控股票可修改 --stocks 参数
rem ============================================================
cd /d D:\CASE-AI量化系统
set PYTHONUTF8=1
set TRADER_DRY_RUN=0
title AI量化系统 - 真实交易循环(live_loop)
.venv\Scripts\python.exe -X utf8 live_trading/live_loop.py --stocks "600519.SH,513100.SH" --interval 60
pause