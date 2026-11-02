@echo off
rem Without installing (Windows): run.cmd (wallpaper) | run.cmd window | run.cmd stop | run.cmd run -f
rem Needs Python 3.11+ and: py -m pip install psutil pycairo pygame-ce
cd /d "%~dp0"
python -m kingdom %*
