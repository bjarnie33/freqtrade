@echo off
rem Heldur ssh-gongunum til freqtrade-lon1 opnum og endurtengir ef thaer falla.
rem ssh-villur fara i tunnel.log (naest thegar eitthvad klikkar sest thar af hverju).
set LOG=%~dp0tunnel.log
:loop
curl.exe -s -f -o NUL --max-time 3 http://localhost:15050/ && goto healthy
curl.exe -s -f -o NUL --max-time 3 http://localhost:15051/ && goto healthy
goto start
:healthy
ping -n 31 127.0.0.1 >nul
goto loop
:start
echo [%date% %time%] raesi ssh -N freqtrade-view>>"%LOG%"
ssh -N -o ServerAliveInterval=30 -o ServerAliveCountMax=3 freqtrade-view 2>>"%LOG%"
echo [%date% %time%] ssh haetti med kodann %errorlevel%>>"%LOG%"
ping -n 16 127.0.0.1 >nul
goto loop
