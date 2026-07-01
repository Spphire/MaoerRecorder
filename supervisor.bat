@echo off
REM Supervisor launcher. Keeps the recorder alive across crashes/freezes.
REM Usage: supervisor.bat [room_id]
setlocal
cd /d "%~dp0"
set ROOM=%1
if "%ROOM%"=="" set ROOM=868802213
py supervisor.py %ROOM%
endlocal
