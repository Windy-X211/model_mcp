@echo off
cd /d "%~dp0"
set "PYW=F:\anaconda3\pythonw.exe"
if not exist "%PYW%" set "PYW=F:\anaconda3\python.exe"
if not exist "%PYW%" set "PYW=pythonw"
start "" "%PYW%" -m model_mcp.tk_ui --port 8765
