@echo off
rem ============================================================
rem 启动模拟盘交易循环 (live_loop.py)  TRADER_DRY_RUN=1 = 模拟, 不下单
rem   :. 用于安全验证信号/风控, 不会真实成交
rem ============================================================
cd /d D:\CASE-AI量化系统
set PYTHONUTF8=1
set TRADER_DRY_RUN=1
title AI量化系统 - 模拟盘循环(live_loop, 安全模式)
.venv\Scripts\python.exe -X utf8 live_trading/live_loop.py --stocks "600519.SH,513100.SH" --interval 60
pause