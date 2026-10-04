@echo off
rem TokenTier installer for Windows. Runs install.ps1 even when the PowerShell execution
rem policy blocks scripts. All arguments are passed through, e.g.  install.cmd --dry-run
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1" %*
exit /b %ERRORLEVEL%
