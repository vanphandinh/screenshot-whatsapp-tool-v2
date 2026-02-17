@echo off
setlocal
title WhatsApp Tool - Environment Setup

echo ======================================================
echo    WhatsApp Tool - Environment Setup
echo ======================================================
echo.

:: Check for Python
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo [ERROR] Python is not installed or not in PATH.
    echo Please install Python and try again.
    pause
    exit /b 1
)

:: Create Virtual Environment
if not exist "venv" (
    echo [*] Creating virtual environment...
    python -m venv venv
    if %errorlevel% neq 0 (
        echo [ERROR] Failed to create virtual environment.
        pause
        exit /b 1
    )
    echo [+] Virtual environment created.
) else (
    echo [!] Virtual environment already exists.
)

:: Activate Venv and Install Dependencies
echo [*] Activating virtual environment...
call venv\Scripts\activate

echo [*] Upgrading pip...
python -m pip install --upgrade pip

echo [*] Installing dependencies from requirements.txt...
pip install -r requirements.txt
if %errorlevel% neq 0 (
    echo [ERROR] Failed to install dependencies.
    pause
    exit /b 1
)

echo.
echo ======================================================
echo [+] Setup completed successfully!
echo [*] You can now run:
echo     1. run_server.bat    (to start the capture server)
echo     2. run_dashboard.bat (to manage WhatsApp sessions)
echo     3. run_get_groups.bat (to get group ids)
echo ======================================================
echo.
pause
