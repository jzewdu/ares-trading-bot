@echo off
rem Keep the desk alive. If the process dies for any reason, start it again in
rem 15 seconds and append the reason to logs\ares.log.
setlocal
cd /d "%~dp0.."
if not exist logs mkdir logs
:loop
rem If something already answers on the dashboard port, this copy is a
rem duplicate (a logon task plus a manual start). Wait instead of fighting for
rem the socket and hammering Alpaca with retries.
netstat -ano | findstr ":8090" | findstr LISTENING >nul
if not errorlevel 1 (
    timeout /t 60 /nobreak <nul >nul
    goto loop
)
echo [%DATE% %TIME%] starting Ares>> logs\ares.log
".venv\Scripts\python.exe" app.py>> logs\ares.log 2>&1
echo [%DATE% %TIME%] Ares exited with %ERRORLEVEL%; restarting in 15s>> logs\ares.log
rem <nul matters: with stdin redirected the child would fail instantly and the
rem loop would spin, hammering the broker with retries.
timeout /t 15 /nobreak <nul >nul
goto loop
