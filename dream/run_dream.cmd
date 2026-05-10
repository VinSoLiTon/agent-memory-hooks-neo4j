@echo off
setlocal
cd /d "%~dp0\.."
if not exist "%~dp0logs" mkdir "%~dp0logs"
set "LOG=%~dp0logs\dream_%date:~10,4%-%date:~4,2%-%date:~7,2%.log"
echo [%date% %time%] dream run start >> "%LOG%"
python dream\dream.py --since 36h >> "%LOG%" 2>&1
echo [%date% %time%] dream run end exit=%errorlevel% >> "%LOG%"
