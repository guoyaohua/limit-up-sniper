@echo off
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0run.ps1" -Mode simulation -ExperimentId expanded-pool-v1 -ChallengerProfile "%~dp0..\config\challengers\expanded-pool-v1.json" -ArchiveTicks %*
set "exit_code=%ERRORLEVEL%"
if not "%exit_code%"=="0" pause
exit /b %exit_code%
