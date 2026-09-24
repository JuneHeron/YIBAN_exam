@echo off
rem ==========================================================
rem  Student handbook quiz bank - double click this file to start.
rem  Keep this file ASCII-only: cmd reads .bat in the local OEM
rem  codepage, so non-ASCII here can break parsing on some systems.
rem  All the Chinese guidance is printed by the Python service.
rem  Do NOT add "chcp" here: it swallows stdin (bitten before).
rem ==========================================================
cd /d "%~dp0"

set PY=
where python >nul 2>nul
if not errorlevel 1 set PY=python
if not defined PY goto trypy
goto run

:trypy
where py >nul 2>nul
if not errorlevel 1 set PY=py
if not defined PY goto nopy

:run
%PY% bank.py serve --open
echo.
pause
exit /b 0

:nopy
echo.
echo   Python not found. Install Python 3 first (one time, ~30MB):
echo.
echo     1. Open  https://www.python.org/downloads/
echo     2. Download the Windows installer and run it
echo     3. IMPORTANT: tick "Add python.exe to PATH" on the first screen
echo     4. Run this file again after installing
echo.
pause
exit /b 1
