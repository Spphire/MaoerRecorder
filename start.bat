@echo off
REM Windows one-click launcher.
REM Usage: start.bat [room_id]
setlocal
cd /d "%~dp0"
set ROOM=%1
if "%ROOM%"=="" set ROOM=868802213
py main.py record --room %ROOM%
endlocal
