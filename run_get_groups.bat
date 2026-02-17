@echo off
setlocal
title WhatsApp Tool - Get Groups
cd /d %~dp0

echo ================================================
echo  WhatsApp Group Viewer
echo ================================================
echo.

if exist venv (
    echo [*] Activating virtual environment...
    call venv\Scripts\activate.bat
) else (
    echo [!] Virtual environment not found. Please run setup_env.bat.
    pause
    exit /b
)

echo.
echo [*] Fetching WhatsApp groups...
python get_groups.py

echo.
echo ================================================
echo Press any key to exit...
pause > nul
exit
