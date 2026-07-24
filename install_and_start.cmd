@echo off
setlocal
title ProGrok Installer
echo Checking and installing the ProGrok runtime. Do not close this window...
echo.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0install_and_start.ps1"
set "EXIT_CODE=%ERRORLEVEL%"
echo.
if "%EXIT_CODE%"=="0" (
  echo Installation and startup completed. Open http://127.0.0.1:3080
) else (
  echo Installation or startup failed. Review the error shown above.
)
echo.
echo Press any key to close this window...
pause >nul
exit /b %EXIT_CODE%
