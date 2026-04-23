@echo off
REM NPU Monitor launcher for Windows.
REM First run creates a venv and installs PySide6; subsequent runs just launch.
REM Prerequisites: Python 3.10+ and the OpenSSH Client (Settings -> Apps -> Optional features).

setlocal
cd /d "%~dp0"

if not exist venv (
    echo Creating virtual environment...
    python -m venv venv
    if errorlevel 1 (
        echo Failed to create venv. Make sure Python 3.10+ is on PATH.
        pause
        exit /b 1
    )
    call venv\Scripts\activate.bat
    echo Installing dependencies...
    python -m pip install --upgrade pip
    pip install -r requirements.txt
) else (
    call venv\Scripts\activate.bat
)

python main.py
if errorlevel 1 pause
endlocal
