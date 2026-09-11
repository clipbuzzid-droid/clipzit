@echo off
REM Clipzit launcher (Windows)
cd /d "%~dp0backend"
if not exist "%~dp0storage\uploads" mkdir "%~dp0storage\uploads"
pip install -q -r "%~dp0requirements.txt"
echo Membuka Clipzit di http://127.0.0.1:8787 ...
start "" "http://127.0.0.1:8787"
python app.py
