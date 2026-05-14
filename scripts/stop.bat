@echo off
:: Stop the backtest platform (Windows)
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0stop.ps1" %*
