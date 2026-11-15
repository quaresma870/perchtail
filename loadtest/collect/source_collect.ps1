# A light collector for a Windows test server: CPU, memory and disk I/O of
# what serves PerchTail (WinRM's wsmprovhost, the SMB server, which runs in
# the System process, and the push agent), every second, so
# `report.py costs --source-csv` can charge each measured operation with
# what it cost on the source. Run it on the server for the length of a run:
#
#   powershell -ExecutionPolicy Bypass -File source_collect.ps1 -Out sources-win-a.csv
#
# Each row: the time (seconds since the epoch, the same clock as the load
# machine's, so keep both in sync), the process name, how many processes
# have it, their CPU time so far (ms, cumulative), working set (bytes), and
# bytes read and written so far (cumulative). Stops on Ctrl+C, or after
# -Seconds.
param(
    [Parameter(Mandatory = $true)][string]$Out,
    [string[]]$Names = @("wsmprovhost", "System", "perchtail-agent"),
    [double]$Interval = 1.0,
    [double]$Seconds = 0
)
$ErrorActionPreference = "Stop"
if (-not (Test-Path $Out)) {
    "t,name,processes,cpu_ms,rss_bytes,read_bytes,write_bytes" | Set-Content -Path $Out -Encoding ascii
}
$deadline = if ($Seconds -gt 0) { (Get-Date).AddSeconds($Seconds) } else { [datetime]::MaxValue }
while ((Get-Date) -lt $deadline) {
    $started = Get-Date
    $t = [math]::Round(([DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()) / 1000.0, 3)
    $lines = foreach ($name in $Names) {
        $procs = @(Get-Process -Name $name -ErrorAction SilentlyContinue)
        $cpu = 0.0; $ws = 0; $read = 0; $write = 0
        foreach ($p in $procs) {
            try { $cpu += $p.TotalProcessorTime.TotalMilliseconds } catch {}
            $ws += $p.WorkingSet64
        }
        $io = Get-CimInstance Win32_Process -Filter "Name = '$name.exe'" -ErrorAction SilentlyContinue
        foreach ($i in @($io)) {
            if ($i) { $read += [int64]$i.ReadTransferCount; $write += [int64]$i.WriteTransferCount }
        }
        "{0},{1},{2},{3:F1},{4},{5},{6}" -f $t, $name, $procs.Count, $cpu, $ws, $read, $write
    }
    $lines | Add-Content -Path $Out -Encoding ascii
    $spent = ((Get-Date) - $started).TotalSeconds
    if ($spent -lt $Interval) { Start-Sleep -Milliseconds ([int](($Interval - $spent) * 1000)) }
}
