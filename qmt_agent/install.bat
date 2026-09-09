@echo off
chcp 65001 >nul
title AI 量化 qmt_agent

REM ============================================================================
REM AI 量化 qmt_agent — 一键启动菜单
REM 双击运行即可选择功能
REM ============================================================================

:check_python
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo [错误] 未找到 Python 3.9+
    echo 请先安装 Python: https://www.python.org/downloads/
    echo 注意: 安装时勾选 "Add Python to PATH"
    pause
    exit /b 1
)

REM 首次运行：安装依赖 + 创建默认配置
if not exist .installed (
    echo ============================================
    echo   首次运行 — 初始化环境
    echo ============================================
    echo.
    echo [1/2] 安装 Python 依赖...
    pip install -r requirements.txt
    if %errorlevel% neq 0 (
        echo [警告] 依赖安装失败，部分功能可能不可用
    )

    echo [2/2] 创建配置...
    if not exist config.yaml (
        (
            echo # qmt_agent 全局配置
            echo qmt_path: "D:\\光大证券金阳光QMT实盘\\userdata_mini"
            echo log_level: "INFO"
            echo host: ""
            echo platform: "win"
            echo.
            echo # 多服务器连接配置
            echo servers:
            echo   - name: "默认服务器"
            echo     server_url: "wss://your-server.com"
            echo     api_key: "your-api-key"
            echo     account_id: ""
            echo     enabled: true
        ) > config.yaml
    )
    mkdir logs 2>nul
    echo. > .installed
    echo.
    echo 初始化完成！请先配置 config.yaml
    echo.
)

:menu
cls
echo ============================================
echo   AI 量化 qmt_agent
echo ============================================
echo.
echo   [1] 配置 qmt_agent（图形界面）
echo   [2] 启动 qmt_agent
echo   [3] 测试连接
echo   [4] 重新安装依赖
echo   [5] 查看日志
echo   [6] 退出
echo.
echo ============================================
set /p choice="请输入选项 (1-6): "

if "%choice%"=="1" goto config
if "%choice%"=="2" goto start
if "%choice%"=="3" goto test
if "%choice%"=="4" goto install
if "%choice%"=="5" goto logs
if "%choice%"=="6" goto exit
echo 无效选项，请重新输入
pause
goto menu

:config
cls
echo ============================================
echo   打开配置窗口
echo ============================================
echo.
python config_gui.py
echo.
echo 配置已保存，返回主菜单...
pause
goto menu

:start
cls
echo ============================================
echo   启动 qmt_agent
echo ============================================
echo.
echo 按 Ctrl+C 可停止 Agent
echo.
python agent.py
pause
goto menu

:test
cls
echo ============================================
echo   测试连接
echo ============================================
echo.
python test_connect.py
echo.
pause
goto menu

:install
cls
echo ============================================
echo   重新安装依赖
echo ============================================
echo.
pip install -r requirements.txt
echo.
echo 安装完成！
pause
goto menu

:logs
cls
echo ============================================
echo   查看日志
echo ============================================
echo.
if exist logs (
    echo 日志目录: logs\
    echo.
    echo 最近日志文件:
    dir /b /o-d logs\ 2>nul
    echo.
    echo 提示: 在资源管理器中打开 logs\ 文件夹查看完整日志
) else (
    echo 暂无日志（Agent 未运行过）
)
echo.
pause
goto menu

:exit
echo 再见！
exit /b 0