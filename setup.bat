@echo off
cd /d "%~dp0"
python -m venv .venv
call .venv\Scripts\activate.bat
python -m pip install --upgrade pip
pip install -r requirements.txt
if not exist ".env" copy ".env.example" ".env"
echo.
echo Setup selesai.
echo Pastikan Ollama sudah terinstall, lalu:
echo   ollama pull deepseek-r1:1.5b
echo.
pause
