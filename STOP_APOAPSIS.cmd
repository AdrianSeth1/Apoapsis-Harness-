@echo off
setlocal
set "APOAPSIS_ROOT=%~dp0"
set "PYTHONPATH=%APOAPSIS_ROOT%src;%PYTHONPATH%"
set "PYTHONUTF8=1"

where py >nul 2>nul
if errorlevel 1 (
  echo Apoapsis needs the Windows Python launcher and Python 3.12 or newer.
  if not defined APOAPSIS_NO_PAUSE pause
  exit /b 1
)

echo Unloading every configured Apoapsis local model...
py -3 -m apoapsis.operator_lifecycle stop --project-root "%APOAPSIS_ROOT%."
set "APOAPSIS_EXIT=%ERRORLEVEL%"
echo.
if "%APOAPSIS_EXIT%"=="0" (
  echo Apoapsis model memory has been released.
) else (
  echo Apoapsis could not complete model cleanup. Review the error above.
)
if not defined APOAPSIS_NO_PAUSE pause
exit /b %APOAPSIS_EXIT%
