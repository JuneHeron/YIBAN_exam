@echo off
rem Do NOT add "chcp" here: chcp swallows stdin, so pasting would read nothing.
rem Do NOT chain a fallback on any errorlevel: that relaunches the program after
rem e.g. Ctrl+C and looks like a hang. Pick the interpreter up front instead.
cd /d "%~dp0"
set PY=python
where python >nul 2>nul
if errorlevel 1 set PY=py
%PY% ask.py
if errorlevel 1 pause
