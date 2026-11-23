@echo off
rem Abre o DiskLens (janela nativa, sem prompt de comando).
cd /d "%~dp0"
call :findpy
start "" "%PYW%" "%~dp0launch.pyw"
exit /b

:findpy
rem pythonw.exe do Python base (lido de .venv\pyvenv.cfg); o lancador do venv pode abrir um console.
set "PYW=%~dp0.venv\Scripts\pythonw.exe"
for /f "tokens=1,* delims==" %%a in ('findstr /b /c:"home" "%~dp0.venv\pyvenv.cfg"') do set "PYHOME=%%b"
if defined PYHOME set "PYHOME=%PYHOME:~1%"
if defined PYHOME if exist "%PYHOME%\pythonw.exe" set "PYW=%PYHOME%\pythonw.exe"
exit /b
