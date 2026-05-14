@echo off
:: Stop the backtest platform (Windows)
:: Delegates to stop.ps1 for reliable process termination
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0stop.ps1" %*
