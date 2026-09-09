@echo off
rem ============================================================
rem 日线增量入库 (大QMT快通道 get_market_data_ex 公式服务)
rem   只处理默认(=今天)当日未入库股票, 占位/异常用新浪兜底
rem   用法: 双击 或 命令行传日期参数 YYYYMMDD (默认今天)
rem   例:   日线增量入库.bat 20260908
rem ============================================================
cd /d D:\CASE-AI量化系统
set PYTHONUTF8=1
title 日线增量入库
.venv\Scripts\python.exe -X utf8 data\sjhq\日线数据-国金QMT入库.py --increment --chunk 100 %1
pause