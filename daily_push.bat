@echo off
chcp 65001 >nul 2>&1
cd /d "C:\Users\29408\Desktop\资产搜索工具"

echo ========================================
echo   每日游戏资讯自动推送
echo   %date% %time%
echo ========================================
echo.

REM 激活虚拟环境（如果有的话）
REM call venv\Scripts\activate.bat

echo [1] 检查依赖...
python -c "import requests, openai; print('  依赖OK')" 2>&1
if errorlevel 1 (
    echo   依赖缺失，正在安装...
    pip install requests openai urllib3 -q
)

echo.
echo [2] 开始采集推送...
python daily_push.py %*

echo.
echo ========================================
echo   完成: %date% %time%
echo ========================================
