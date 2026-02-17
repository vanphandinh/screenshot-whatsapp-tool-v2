@echo off
setlocal
title WhatsApp Tool - Dashboard
cd /d %~dp0

echo ================================================
echo  WPPConnect Dashboard
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
echo Starting dashboard on port 5000...
echo.
python dashboard.py
pause
