@echo off
setlocal
cd /d "%~dp0"
set "A05_RUN_TEMP=%LOCALAPPDATA%\A05Editable\temporal"
if not exist "%A05_RUN_TEMP%" mkdir "%A05_RUN_TEMP%"
set "TEMP=%A05_RUN_TEMP%"
set "TMP=%A05_RUN_TEMP%"
if not exist ".venv\Scripts\python.exe" (
    py -3.12 -m venv .venv
    if errorlevel 1 goto failed
)
".venv\Scripts\python.exe" -m pip install -r "fuente\requirements_diagnostico.txt"
if errorlevel 1 goto failed
if not "%~1"=="" (
    ".venv\Scripts\python.exe" -B tools\preparar_componentes.py --motor-original "%~1"
    if errorlevel 1 goto failed
)
".venv\Scripts\python.exe" -B tools\construir.py
if errorlevel 1 goto failed
echo Construccion completada. No se ejecuto ninguna operacion sobre el telefono.
exit /b 0
:failed
echo No se pudo construir. Revisa los mensajes anteriores y las instrucciones del README.
exit /b 1
