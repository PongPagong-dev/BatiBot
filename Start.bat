@echo off
cd /d "%~dp0"
if not exist venv\Scripts\activate.bat (
    echo Run install.bat first.
    pause
    exit /b 1
)
call venv\Scripts\activate.bat
echo Starting BatiBot - UI at http://127.0.0.1:8099
python main.py
pause
