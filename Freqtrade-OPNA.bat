@echo off
cd /d "%~dp0"
python freqtrade_opna.py %*
if /i not "%~1"=="--auto" pause
