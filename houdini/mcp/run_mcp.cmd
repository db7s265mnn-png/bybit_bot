@echo off
REM Cursor on Windows may have `py` but not `python`.
cd /d "%~dp0"
py -3 "%~dp0server.py" %*
if errorlevel 1 python "%~dp0server.py" %*
