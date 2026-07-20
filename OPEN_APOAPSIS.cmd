@echo off
setlocal
set "APOAPSIS_ROOT=%~dp0"
set "APOAPSIS_PROJECT=%~1"
if not defined APOAPSIS_PROJECT set "APOAPSIS_PROJECT=%APOAPSIS_ROOT%."
set "PYTHONPATH=%APOAPSIS_ROOT%src;%PYTHONPATH%"
set "PYTHONUTF8=1"

where py >nul 2>nul
if errorlevel 1 (
  echo Apoapsis needs the Windows Python launcher and Python 3.12 or newer.
  echo Install the project prerequisites, then try again.
  if not defined APOAPSIS_NO_PAUSE pause
  exit /b 1
)

where git >nul 2>nul
if errorlevel 1 (
  echo Apoapsis needs Git on PATH to inspect the repository.
  echo Install Git, then try again.
  if not defined APOAPSIS_NO_PAUSE pause
  exit /b 1
)

if not exist "%APOAPSIS_PROJECT%\.git" (
  echo The selected folder is not a Git repository:
  echo   %APOAPSIS_PROJECT%
  echo Create or clone a Git repository first, then try again.
  if not defined APOAPSIS_NO_PAUSE pause
  exit /b 1
)

if not exist "%APOAPSIS_PROJECT%\.apoapsis\config.toml" (
  echo This project has not been initialized for Apoapsis yet:
  echo   %APOAPSIS_PROJECT%
  echo From a terminal in that folder, run: apoapsis init
  echo Then reopen this launcher.
  if not defined APOAPSIS_NO_PAUSE pause
  exit /b 1
)

echo Opening the Apoapsis local interface in your system browser...
echo Project: %APOAPSIS_PROJECT%
echo This window runs the Apoapsis UI server only. Closing it, or pressing
echo Ctrl+C, stops just this UI process -- it does not unload any model or
echo change Docker/Ollama settings.
echo To release local model memory when you are done, use STOP_APOAPSIS.cmd.
echo.
py -3 -m apoapsis.cli.app --project-root "%APOAPSIS_PROJECT%" ui
set "APOAPSIS_EXIT=%ERRORLEVEL%"
echo.
if not "%APOAPSIS_EXIT%"=="0" (
  echo Apoapsis UI exited with an error. Review the output above.
  echo For debugging or automation, the CLI remains available directly:
  echo   apoapsis ui
)
if not defined APOAPSIS_NO_PAUSE pause
exit /b %APOAPSIS_EXIT%
