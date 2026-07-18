@echo off
setlocal
set "APOAPSIS_ROOT=%~dp0"
set "PYTHONPATH=%APOAPSIS_ROOT%src;%PYTHONPATH%"
set "PYTHONUTF8=1"

where py >nul 2>nul
if errorlevel 1 (
  echo Apoapsis needs the Windows Python launcher and Python 3.12 or newer.
  echo Install the project prerequisites, then try again.
  if not defined APOAPSIS_NO_PAUSE pause
  exit /b 1
)

echo Starting Apoapsis local coding models...
py -3 -m apoapsis.operator_lifecycle start --project-root "%APOAPSIS_ROOT%." %*
set "APOAPSIS_EXIT=%ERRORLEVEL%"
echo.
if "%APOAPSIS_EXIT%"=="0" (
  echo Apoapsis models are ready.
) else (
  echo Apoapsis could not start cleanly. Review the error above.
)
if not defined APOAPSIS_NO_PAUSE pause
exit /b %APOAPSIS_EXIT%
