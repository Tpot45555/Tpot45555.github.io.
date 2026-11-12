@echo off
title Atlas de mi PC
cd /d "%~dp0"
where py >nul 2>nul
if %errorlevel%==0 (py -3 ciudad.py) else (python ciudad.py)
if errorlevel 1 pause
