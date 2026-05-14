@echo off
:: Start the backtest platform (Windows)
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start.ps1" %*
