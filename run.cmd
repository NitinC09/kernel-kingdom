@echo off
rem Sin instalar (Windows): run.cmd (fondo de pantalla) | run.cmd window | run.cmd stop | run.cmd run -f
rem Necesita Python 3.11+ y: py -m pip install psutil pycairo pygame-ce
cd /d "%~dp0"
python -m reino %*
