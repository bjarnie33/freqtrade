@echo off
chcp 65001 >nul
cd /d "%~dp0"

echo === Athuga hvort Docker Desktop er i gangi ===
docker info >nul 2>&1
if %errorlevel% equ 0 goto dockerready

echo Docker Desktop er ekki i gangi - reyni ad raesa thad...
start "" "C:\Program Files\Docker\Docker\Docker Desktop.exe"
set tries=0

:waitloop
timeout /t 5 /nobreak >nul
docker info >nul 2>&1
if %errorlevel% equ 0 goto dockerready
set /a tries+=1
if %tries% geq 24 goto dockerfail
echo Bid eftir Docker Desktop... (%tries%/24)
goto waitloop

:dockerfail
echo.
echo Docker Desktop for ekki i gang eftir 2 minutur.
echo Opnadu Docker Desktop handvirkt, bidddu thar til thad er tilbuid, og keyrdu thetta skjal aftur.
pause
exit /b 1

:dockerready
echo === Docker er i gangi ===
echo.
echo === Raesi Freqtrade ===
docker compose up -d

echo.
echo === Bid i 10 sekundur svo Freqtrade nai ad klara raesingu ===
timeout /t 10 /nobreak

echo.
echo === Raesi maelabordid ===
echo (Ef thu lokar thessum glugga stodvast bara maelabordid - Freqtrade heldur afram ad keyra i Docker)
echo Opnadu http://localhost:5050
echo.
python3 Freqtrade_dashboard.py

pause