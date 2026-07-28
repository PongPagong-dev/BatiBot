@echo off
echo Fixing OCR engine versions (paddlepaddle 3.x has a bug - pinning 2.6.2)...
cd /d "%~dp0"
call venv\Scripts\activate.bat
pip install paddlepaddle==2.6.2 paddleocr==2.10.0 numpy==1.26.4
echo.
echo Done. Run Start.bat again.
pause
