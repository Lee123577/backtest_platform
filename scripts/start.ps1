# Start the backtest platform (Windows PowerShell)

# Project root is one level up from this script
$RootDir  = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$PidFile  = Join-Path $RootDir "app.pid"
$LogFile  = Join-Path $RootDir "output.log"

# Check if already running
if (Test-Path $PidFile) {
    $savedPid = [int](Get-Content $PidFile -Raw).Trim()
    if (Get-Process -Id $savedPid -ErrorAction SilentlyContinue) {
        Write-Host "[INFO] Service is already running (PID: $savedPid)"
        Write-Host "[INFO] Log: $LogFile"
        exit 0
    }
    Write-Host "[WARN] Stale PID file found, cleaning up..."
    Remove-Item $PidFile
}

# Use cmd /c to merge stdout+stderr into the log file
$proc = Start-Process cmd `
    -ArgumentList "/c", "python run.py >> `"$LogFile`" 2>&1" `
    -WorkingDirectory $RootDir `
    -WindowStyle Hidden `
    -PassThru

Start-Sleep -Seconds 2

if ($proc.HasExited) {
    Write-Host "[ERR] Service failed to start. Check log: $LogFile"
    exit 1
}

# Save PID (taskkill /t will kill the python child too)
$proc.Id | Out-File $PidFile -Encoding ascii -NoNewline
Write-Host "[OK] Service started (PID: $($proc.Id))"
Write-Host "[OK] Log: $LogFile"
