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

rem The project whose models should be released is the one START_APOAPSIS.cmd
rem opened, not the Apoapsis install directory. Reading "%APOAPSIS_ROOT%."
rem silently unloaded whatever the harness repo happened to configure.
set "APOAPSIS_PROJECT=%~1"
if defined APOAPSIS_PROJECT goto PROJECT_SELECTED

for /f "usebackq delims=" %%P in (`powershell -NoProfile -STA -ExecutionPolicy Bypass -Command "Add-Type -AssemblyName System.Windows.Forms; $dialog = New-Object System.Windows.Forms.FolderBrowserDialog; $dialog.Description = 'Select the Apoapsis project whose local models should be released'; $dialog.ShowNewFolderButton = $false; if ($dialog.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) { $dialog.SelectedPath }"`) do set "APOAPSIS_PROJECT=%%P"
if not defined APOAPSIS_PROJECT (
  echo No project folder was selected.
  if not defined APOAPSIS_NO_PAUSE pause
  exit /b 1
)

:PROJECT_SELECTED
if not exist "%APOAPSIS_PROJECT%\.apoapsis\config.toml" (
  echo This project has not been initialized for Apoapsis:
  echo   %APOAPSIS_PROJECT%
  if not defined APOAPSIS_NO_PAUSE pause
  exit /b 1
)

echo Unloading every configured Apoapsis local model for:
echo   %APOAPSIS_PROJECT%
py -3 -m apoapsis.operator_lifecycle stop --project-root "%APOAPSIS_PROJECT%"
set "APOAPSIS_EXIT=%ERRORLEVEL%"
echo.
if "%APOAPSIS_EXIT%"=="0" (
  echo Apoapsis model memory has been released.
) else (
  echo Apoapsis could not complete model cleanup. Review the error above.
)
if not defined APOAPSIS_NO_PAUSE pause
exit /b %APOAPSIS_EXIT%
