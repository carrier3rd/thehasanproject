# Shared by daemons.ps1 and tasks.ps1 (Nov 27 2026; same file in socialscope-private/ops/pc). Dot-source it.
#
# Two ways a task can run:
#   desktop (default, the owner's PC): only while the owner is logged in (Interactive), started at logon.
#   server  (-Server, the server laptop): "whether the user is logged on or not" WITHOUT a stored password (S4U), started
#           at boot. Tested Nov 27 2026 on the laptop: such a task has the internet, the gh and rclone logins, the venvs
#           and claude -p. It can't use Windows network logins (shared folders), which no job needs.

function Find-Python {
  foreach ($p in "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe", "C:\Program Files\Python312\python.exe") {
    if (Test-Path $p) { return $p }
  }
  return (& py -3.12 -c "import sys; print(sys.executable)")
}

function Quote([string]$s) { if ($s -match '[\s"]') { '"' + ($s -replace '"', '\"') + '"' } else { $s } }

# Register-Job NAME ARGV WORKDIR TRIGGERS [-Daemon] [-Priority N] [-LimitHours H] [-Server]
#   ARGV: program and its arguments. Daemons run without a time limit and are re-launched every 5 minutes if they
#   died (keep_running.py makes a second copy exit at once).
function Register-Job {
  param([string]$Name, [string[]]$Argv, [string]$WorkDir, $Triggers = @(), [switch]$Daemon, [int]$Priority = 6,
        [double]$LimitHours = 6, [switch]$Server)
  $u = "$env:USERDOMAIN\$env:USERNAME"
  if ($Server) {
    # no console window exists in a non-interactive session: run the program directly
    $a = New-ScheduledTaskAction -Execute $Argv[0] -Argument (($Argv[1..($Argv.Count - 1)] | % { Quote $_ }) -join " ") -WorkingDirectory $WorkDir
    $p = New-ScheduledTaskPrincipal -UserId $u -LogonType S4U -RunLevel Highest
  } else {
    $a = New-ScheduledTaskAction -Execute "C:\Windows\System32\conhost.exe" -Argument ("--headless " + (($Argv | % { Quote $_ }) -join " ")) -WorkingDirectory $WorkDir
    $p = New-ScheduledTaskPrincipal -UserId $u -LogonType Interactive -RunLevel Limited
  }
  $t = @($Triggers)
  if ($Daemon) {
    $t += if ($Server) { New-ScheduledTaskTrigger -AtStartup } else { New-ScheduledTaskTrigger -AtLogOn -User $u }
    $t += New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 5)
    $limit = [TimeSpan]::Zero
  } else {
    $limit = New-TimeSpan -Minutes ([int]($LimitHours * 60))
  }
  $s = New-ScheduledTaskSettingsSet -ExecutionTimeLimit $limit -MultipleInstances IgnoreNew -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries -StartWhenAvailable -Priority $Priority
  Register-ScheduledTask -TaskName $Name -Action $a -Trigger $t -Settings $s -Principal $p -Force | Out-Null
  "registered: $Name" + $(if ($Server) { ' (server: at boot, no login needed)' } else { '' })
}

function Every-Minutes([int]$Minutes, [int]$Offset = 0) {
  # repeats forever, first run at the next multiple of $Minutes plus $Offset minutes
  $now = Get-Date
  $start = $now.Date.AddHours($now.Hour).AddMinutes($Offset)
  while ($start -le $now) { $start = $start.AddMinutes($Minutes) }
  New-ScheduledTaskTrigger -Once -At $start -RepetitionInterval (New-TimeSpan -Minutes $Minutes)
}
