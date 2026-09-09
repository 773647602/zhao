@echo off
rem ============================================================
rem AI量化系统 - 一键启动脚本
rem
rem 启动顺序:
rem   1. start_web.bat       Web服务 + 选股调度 (app.py / :7865)
rem   2. start_scheduler.bat 定时任务 (scheduler.py)
rem   3. start_live.bat      真实交易循环 (live_loop.py, TRADER_DRY_RUN=0)
rem
rem 说明:
rem   - 每个组件独立窗口, 可单独关闭排除问题
rem   - 若只想跑模拟盘, 把最后一行改 start_live_sim.bat (不下单)
rem ============================================================
cd /d D:\CASE-AI量化系统

rem ---- 1) Web + 选股调度 ----
echo [1/3] 启动 Web 服务 + 选股调度 (app.py) ...
start "AI量化-Web选股" cmd /c call "D:\CASE-AI量化系统\start_web.bat"

echo.
timeout /t 3 /nobreak >nul

rem ---- 2) 定时任务 ----
echo [2/3] 启动定时任务 (scheduler.py) ...
start "AI量化-定时任务" cmd /c call "D:\CASE-AI量化系统\start_scheduler.bat"

echo.
timeout /t 2 /nobreak >nul

rem ---- 3) 真实交易循环 ----
rem 安全提示: 下面使用真实交易模式(会真实下单). 若要模拟盘, 注释掉本行, 取消下一行注释.
echo [3/3] 启动真实交易循环 (live_loop.py, TRADER_DRY_RUN=0) ...
start "AI量化-实盘循环" cmd /c call "D:\CASE-AI量化系统\start_live.bat"
rem start "AI量化-模拟循环" cmd /c call "D:\CASE-AI量化系统\start_live_sim.bat"

echo.
echo 启动完成! Web 访问: http://127.0.0.1:7865
echo 三个窗口正在各自运行中, 请勿关闭.
pause