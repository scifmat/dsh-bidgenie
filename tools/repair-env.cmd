@echo off
rem BidGenie pylibs emergency repair launcher (bypasses local script execution policy).
rem Usage: repair-env.cmd            -> repair
rem        repair-env.cmd -DryRun    -> print plan only
rem        repair-env.cmd -Rollback  -> restore last pylibs.broken-* backup
setlocal
set "SCRIPT=%~dp0repair-bidgenie-pylibs.ps1"
if not exist "%SCRIPT%" (
  echo [FAIL] cannot find "%SCRIPT%"
  pause
  exit /b 1
)
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%SCRIPT%" %*
set "RC=%ERRORLEVEL%"
echo.
echo exit code: %RC%
if "%~1"=="" pause
exit /b %RC%
