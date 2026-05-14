@echo off
:: Start the backtest platform (Windows)
:: Delegates to start.ps1 for reliable PID tracking
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start.ps1" %*
