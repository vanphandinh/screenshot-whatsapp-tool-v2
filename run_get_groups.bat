@echo off
setlocal
cd /d "%~dp0"

if not exist venv (
    echo [!] Virtual environment not found. Please run setup_env.bat first.
    pause
    exit /b
)

echo [*] Activating virtual environment...
call venv\Scripts\activate.bat

echo [*] Fetching WhatsApp groups...
python get_groups.py

echo.
echo Press any key to exit...
pause > nul
deactivate
