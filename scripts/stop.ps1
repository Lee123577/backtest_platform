# Stop the backtest platform (Windows PowerShell)

# Project root is one level up from this script
$RootDir = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$PidFile = Join-Path $RootDir "app.pid"

function Stop-ByPid($procId) {
    $proc = Get-Process -Id $procId -ErrorAction SilentlyContinue
    if ($proc) {
        # /t kills the entire process tree (cmd wrapper + python child)
        & taskkill /pid $procId /f /t | Out-Null
        Write-Host "[OK] Service stopped (PID: $procId)"
    } else {
        Write-Host "[WARN] Process $procId is not running"
    }
    if (Test-Path $PidFile) { Remove-Item $PidFile }
}

if (Test-Path $PidFile) {
    $savedPid = [int](Get-Content $PidFile -Raw).Trim()
    Stop-ByPid $savedPid
} else {
    Write-Host "[WARN] PID file not found, searching for python run.py process..."
    $found = Get-WmiObject Win32_Process -Filter "Name='python.exe'" |
        Where-Object { $_.CommandLine -like "*run.py*" }
    if ($found) {
        foreach ($p in $found) {
            & taskkill /pid $p.ProcessId /f /t | Out-Null
            Write-Host "[OK] Stopped process (PID: $($p.ProcessId))"
        }
    } else {
        Write-Host "[INFO] No running service found."
    }
}
