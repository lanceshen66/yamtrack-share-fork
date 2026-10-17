# check-daemon.ps1
# Purpose : print how many Yamtrack Redis keeper processes are running (normally 0 or 1).
# Called  : by start.bat, to decide whether the keeper needs to be launched.
# Filter  : requires BOTH the executable to be this project's .venv python.exe
#           AND the command line to mention redis-daemon.py, so the querying
#           process itself is never counted.
# NOTE    : keep this file pure ASCII. Windows PowerShell 5.1 reads .ps1 files
#           without a BOM as ANSI, which would garble non-ASCII text.

$n = 0
Get-CimInstance Win32_Process | ForEach-Object {
    if ($_.ExecutablePath -like '*Yamtrack*.venv*python.exe' -and $_.CommandLine -like '*redis-daemon.py*') {
        $n++
    }
}
Write-Output $n
