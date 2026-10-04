@echo off
rem TokenTier uninstaller for Windows. Runs uninstall.ps1 even when the PowerShell execution
rem policy blocks scripts. All arguments are passed through, e.g.  uninstall.cmd --dry-run
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0uninstall.ps1" %*
exit /b %ERRORLEVEL%
