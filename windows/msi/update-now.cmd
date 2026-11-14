@echo off
rem The Start menu's "Update uBlock Origin now": the same install the daily task
rem runs, in a console, so its output and prompts are visible.
title Update uBlock Origin
"%~dp0python\python.exe" "%~dp0ubo-lpa.py" install
echo.
pause
