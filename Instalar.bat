@echo off
rem Prepara o DiskLens neste computador: cria o ambiente Python (.venv) e instala o pywebview.
rem Requer Python 3.12 ou mais novo (https://www.python.org/downloads/).
cd /d "%~dp0"

set PY=
where py >nul 2>nul && set PY=py -3
if not defined PY where python >nul 2>nul && set PY=python
if not defined PY (
  echo Python nao encontrado. Instale o Python 3.12+ em https://www.python.org/downloads/
  echo e marque a opcao "Add python.exe to PATH" durante a instalacao.
  pause
  exit /b 1
)

%PY% -c "import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)"
if errorlevel 1 (
  echo O DiskLens precisa do Python 3.12 ou mais novo. Versao encontrada:
  %PY% --version
  pause
  exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
  echo Criando o ambiente .venv ...
  %PY% -m venv .venv || (echo Falha ao criar o ambiente. & pause & exit /b 1)
)
echo Instalando dependencias ...
".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -q -r requirements.txt || (echo Falha ao instalar. & pause & exit /b 1)

echo.
echo Pronto! Abra o DiskLens com "DiskLens (administrador).bat" (recomendado) ou "DiskLens.bat".
pause
