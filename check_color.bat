@echo off
setlocal
title Binance 颜色测试
echo ============================================
echo   检查终端 ANSI 颜色支持
echo ============================================
python -c "import api_client; api_client.enable_vt(); print('\033[1;33m=== 金色测试 OK ===\033[0m'); print('\033[1;32m=== 绿色测试 OK ===\033[0m'); print('普通文字')"
echo.
echo ============================================
echo   如果上面两行有颜色显示，则终端支持 ANSI
echo   如果显示乱码(^[1;33m 之类)，则终端不支持颜色
echo ============================================
pause