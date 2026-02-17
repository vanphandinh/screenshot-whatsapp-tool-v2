@echo off
echo Starting WPPConnect Dashboard...
cd /d %~dp0
call venv\Scripts\activate
python dashboard.py
pause
