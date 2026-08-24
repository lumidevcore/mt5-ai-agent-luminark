@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Jalankan setup.bat dulu.
  pause
  exit /b 1
)
call .venv\Scripts\activate.bat
python main.py
pause
