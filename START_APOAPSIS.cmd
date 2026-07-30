@echo off
setlocal EnableExtensions
set "APOAPSIS_ROOT=%~dp0"
set "APOAPSIS_PROJECT=%~1"
set "APOAPSIS_LIFECYCLE_ARGS="
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

rem SHIFT renumbers %1/%2 but does NOT change %*, so the remaining arguments
rem have to be rebuilt one at a time. Using "%*" after a SHIFT re-sends the
rem project folder as a stray positional and argparse rejects the whole call.
if not defined APOAPSIS_PROJECT goto PICK_PROJECT
if "%APOAPSIS_PROJECT:~0,1%"=="-" (
  set "APOAPSIS_PROJECT="
  goto COLLECT_ARGS
)
shift /1

:COLLECT_ARGS
if "%~1"=="" goto COLLECT_DONE
set "APOAPSIS_LIFECYCLE_ARGS=%APOAPSIS_LIFECYCLE_ARGS% %1"
shift /1
goto COLLECT_ARGS

:COLLECT_DONE
if not defined APOAPSIS_PROJECT goto PICK_PROJECT
goto PROJECT_SELECTED

:PICK_PROJECT
for /f "usebackq delims=" %%P in (`powershell -NoProfile -STA -ExecutionPolicy Bypass -Command "Add-Type -AssemblyName System.Windows.Forms; $dialog = New-Object System.Windows.Forms.FolderBrowserDialog; $dialog.Description = 'Select the Git project to open in Apoapsis'; $dialog.ShowNewFolderButton = $false; if ($dialog.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) { $dialog.SelectedPath }"`) do set "APOAPSIS_PROJECT=%%P"
if not defined APOAPSIS_PROJECT (
  echo No project folder was selected.
  if not defined APOAPSIS_NO_PAUSE pause
  exit /b 1
)

:PROJECT_SELECTED
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
  echo Run apoapsis init in that repository once, then reopen this launcher.
  if not defined APOAPSIS_NO_PAUSE pause
  exit /b 1
)

echo Starting Apoapsis local coding service for:
echo   %APOAPSIS_PROJECT%
py -3 -m apoapsis.operator_lifecycle start --project-root "%APOAPSIS_PROJECT%" %APOAPSIS_LIFECYCLE_ARGS%
set "APOAPSIS_EXIT=%ERRORLEVEL%"
echo.
if not "%APOAPSIS_EXIT%"=="0" (
  echo Apoapsis could not start the configured local model cleanly.
  echo If this project uses llama-server, set APOAPSIS_LLAMA_SERVER_COMMAND
  echo to the explicit command that starts Laguna, then try again.
  if not defined APOAPSIS_NO_PAUSE pause
  exit /b %APOAPSIS_EXIT%
)

echo Opening the Apoapsis local interface...
py -3 -m apoapsis.cli.app --project-root "%APOAPSIS_PROJECT%" ui
set "APOAPSIS_EXIT=%ERRORLEVEL%"
echo.
if not "%APOAPSIS_EXIT%"=="0" (
  echo Apoapsis UI exited with an error. Review the output above.
)
if not defined APOAPSIS_NO_PAUSE pause
exit /b %APOAPSIS_EXIT%
