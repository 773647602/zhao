@echo off
rem ============================================================
rem 日线历史区间回补 (大QMT快通道, 按日期幂等补齐缺失)
rem   用法(必填起止, 含): 日线历史回补.bat 20260815 20260908
rem   按 (stock_code, trade_date) upsert, 不整只跳过
rem ============================================================
cd /d D:\CASE-AI量化系统
set PYTHONUTF8=1
title 日线历史回补
.venv\Scripts\python.exe -X utf8 data\sjhq\日线数据-国金QMT入库.py --gap-fill --chunk 100 %1 %2
pause