@echo off
echo ============================================
echo  UMA IT BOT - one-time install
echo  Requires: Python 3.10+ on PATH, MuMu Player
echo ============================================
cd /d "%~dp0"
python --version >nul 2>&1
if errorlevel 1 (
    echo ERROR: Python not found. Install Python 3.10+ from python.org
    echo        and tick "Add python.exe to PATH" during setup.
    pause
    exit /b 1
)
python -m venv venv
call venv\Scripts\activate.bat
python -m pip install --upgrade pip
pip install -r requirements.txt
echo.
echo Install complete. Run Start.bat to launch the bot.
pause
