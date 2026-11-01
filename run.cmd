@echo off
rem Sin instalar (Windows): run.cmd window | run.cmd stop | run.cmd run -f
rem Necesita el Python de MSYS2 UCRT64 con GTK 4 y PyGObject (ver install.ps1).
if exist "C:\msys64\ucrt64\bin\python.exe" set "PATH=C:\msys64\ucrt64\bin;%PATH%"
cd /d "%~dp0"
python -m reino %*
