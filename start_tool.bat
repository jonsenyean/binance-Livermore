@echo off
setlocal
title Binance 利弗莫尔战法滚仓策略 · Paper Trading Launcher
cd /d "%~dp0"

:menu
cls
echo ==========================================
echo   币安永续 · 利弗莫尔战法滚仓策略 · 纸面交易工具 (Paper Trading)
echo ==========================================
echo   1. 启动纸面交易工具
echo   2. 运行离线自测
echo   3. 安装/更新依赖
echo   4. 清理缓存
echo   0. 退出
echo ==========================================
set "choice="
set /p choice=请输入数字并回车:
set "choice=%choice: =%" & REM 去掉输入中的空格，兼容多空格/脚本

if "%choice%"=="1" goto :run
if "%choice%"=="2" goto :test
if "%choice%"=="3" goto :install
if "%choice%"=="4" goto :clean
if "%choice%"=="0" goto :eof
echo 无效选项，请重新输入。
pause
goto :menu

:run
call :check_python || goto :fail
python main.py
echo.
pause
goto :menu

:test
call :check_python || goto :fail
python test_logic.py
echo.
pause
goto :menu

:install
call :check_python || goto :fail
python -m pip install -r requirements.txt
echo.
pause
goto :menu

:clean
if exist __pycache__ rmdir /s /q __pycache__
echo 缓存已清理。
pause
goto :menu

:check_python
python --version >nul 2>&1
if errorlevel 1 (
    echo.
    echo [错误] 未检测到 Python，请先安装 Python 3.8 以上版本。
    echo    https://www.python.org/downloads/
    echo    Windows 安装时请勾选 "Add Python to PATH"
    echo.
    exit /b 1
)
exit /b 0

:fail
pause
goto :menu
