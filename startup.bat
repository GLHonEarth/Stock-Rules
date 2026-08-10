@echo off
chcp 65001 >nul
title 股票智能技术分析与策略决策系统
cd /d %~dp0

echo ============================================
echo  股票智能技术分析与策略决策系统 - 启动中...
echo ============================================
echo.

rem 检查 Python
where python >nul 2>nul
if errorlevel 1 (
    echo [错误] 未检测到 Python，请先安装 Python 3.9+（推荐 Anaconda）
    pause
    exit /b 1
)

rem 检查依赖，缺失则自动安装（国内网络自动使用清华镜像加速）
python -c "import streamlit, akshare, pandas, numpy, plotly, requests" >nul 2>nul
if errorlevel 1 (
    echo [提示] 首次运行，正在安装依赖（约需 2-5 分钟，请耐心等待）...
    pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
    if errorlevel 1 (
        echo [提示] 镜像安装失败，改用默认源重试...
        pip install -r requirements.txt
    )
)

echo.
echo [提示] 启动成功后浏览器将自动打开 http://localhost:8501
echo        若未自动打开，请手动在浏览器访问该地址。
echo        关闭本窗口即可停止系统。
echo.
streamlit run app.py
pause
