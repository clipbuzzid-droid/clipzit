@echo off
REM Clipzit test suite
cd /d "%~dp0"
pip show pytest >nul 2>&1 || pip install -q pytest
python -m pytest tests/ -q --tb=short
