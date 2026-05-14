# Start the backtest platform (Windows PowerShell)
param(
    [int]$Port = 8000
)

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$PidFile   = Join-Path $ScriptDir "app.pid"
$LogFile   = Join-Path $ScriptDir "output.log"

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
    -WorkingDirectory $ScriptDir `
    -WindowStyle Hidden `
    -PassThru

Start-Sleep -Seconds 2

if ($proc.HasExited) {
    Write-Host "[ERR] Service failed to start. Check log: $LogFile"
    exit 1
}

# Save the cmd wrapper PID (taskkill /t will kill the python child too)
$proc.Id | Out-File $PidFile -Encoding ascii -NoNewline
Write-Host "[OK] Service started (PID: $($proc.Id))"
Write-Host "[OK] Log: $LogFile"
