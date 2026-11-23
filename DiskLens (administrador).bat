@echo off
rem Abre o DiskLens como administrador (le pastas protegidas e reduz o "Nao contabilizado").
cd /d "%~dp0"
set "PYW=%~dp0.venv\Scripts\pythonw.exe"
for /f "tokens=1,* delims==" %%a in ('findstr /b /c:"home" "%~dp0.venv\pyvenv.cfg"') do set "PYHOME=%%b"
if defined PYHOME set "PYHOME=%PYHOME:~1%"
if defined PYHOME if exist "%PYHOME%\pythonw.exe" set "PYW=%PYHOME%\pythonw.exe"
powershell -NoProfile -WindowStyle Hidden -Command "Start-Process -Verb RunAs -WorkingDirectory '%~dp0' -FilePath '%PYW%' -ArgumentList '\"%~dp0launch.pyw\"'"
