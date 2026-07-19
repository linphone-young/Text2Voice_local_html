@echo off
:: 编码格式切换为 UTF-8 杜绝中文乱码
chcp 65001 > nul
echo ===================================================
echo   🚀 欢迎使用 词汇音频生成系统 极速部署向导
echo ===================================================

echo.
echo [1/3] 正在检查并自动安装 Python 核心依赖依赖库...
pip install -r requirements.txt

echo.
echo [2/3] 正在探测系统环境并初始化后端微服务...
echo 💡 提示：如果跨设备访问，请在浏览器中输入 本机IP:8003

echo.
echo [3/3] 正在启动应用服务器...
start http://127.0.0.1:8003
python app.py

pause