@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo 正在启动 model-mcp 桌面窗口（调试模式，报错会显示在这里）...
"F:\anaconda3\python.exe" -m model_mcp.tk_ui --port 8765
echo.
echo 窗口已退出，退出码 %ERRORLEVEL%。日志：logs\tk_ui.log
pause
