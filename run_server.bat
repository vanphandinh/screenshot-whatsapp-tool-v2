@echo off
setlocal
cd /d %~dp0

:: Run the server using pythonw.exe (no console window)
if exist venv\Scripts\pythonw.exe (
    start venv\Scripts\pythonw.exe server.py
) else (
    start pythonw server.py
)

exit
