# Scheduled VOD archive jobs (2026-11-27). Transcription is not here: it needs an NVIDIA GPU and runs on the machine
# that has one (deploy/pc/windows/runner.py, its own tasks).
#
#   powershell -File win\tasks.ps1            register (or re-register) them here (desktop: while logged in)
#   powershell -File win\tasks.ps1 -Server    a server: run whether anyone is logged in or not, no password
#   powershell -File win\tasks.ps1 -Disable   switch them off here (after they moved to another machine)
#
# Priorities follow win/hub.py's levels: 4 publishing, 6 daily upkeep, 7 data collection.
param([switch]$Server, [switch]$Disable)
$ErrorActionPreference = "Stop"
. "$PSScriptRoot\tasklib.ps1"
$Hub = Split-Path -Parent $PSScriptRoot
$Py = "$Hub\.venv\Scripts\python.exe"

# name, arguments after python, trigger, priority, time limit (hours)
$Jobs = @(
  # downloads, then rebuild + publish when something new arrived, then the daily backup
  @("VOD hub publish",  @("win\hub.py", "run"),             (Every-Minutes 60 45), 4, 3),
  # Spotlight, Chat vs Hasan, Receipts, quiz (claude -p on the subscription)
  @("VOD features",     @("win\hub.py", "features"),        (New-ScheduledTaskTrigger -Daily -At 02:30), 6, 6),
  # weekly reel + fact-checks, including the week in progress (claude -p on the subscription; cached, so a quiet
  # hour sends nothing). Starts 30 min after the publish run; a run still going when the next is due is not doubled.
  @("VOD reel",         @("win\hub.py", "reel"),            (Every-Minutes 60 15), 6, 6),
  # posts and comments of the broad subs for the blindspot feature
  @("VOD reddit pull",  @("pipeline\reddit_pull.py"),       (New-ScheduledTaskTrigger -Weekly -DaysOfWeek Tuesday -At 23:00), 7, 6)
)

foreach ($j in $Jobs) {
  $name = $j[0]
  if ($Disable) {
    if (Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue) { Disable-ScheduledTask -TaskName $name | Out-Null; "disabled: $name" }
    continue
  }
  Register-Job -Name $name -Argv (@($Py) + $j[1]) -WorkDir $Hub -Triggers $j[2] -Priority $j[3] -LimitHours $j[4] -Server:$Server
}
