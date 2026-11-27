# Always-on VOD archive programs on the Windows PC (moved from the laptop's systemd services on Nov 26 2026: the
# laptop's RAM corrupts data). Each runs under keep_running.py (restart on exit, one copy, no idle sleep) from a
# scheduled task that starts at logon and re-launches it every 5 minutes if it died (a running copy makes the
# re-launch exit at once).
#
#   powershell -File win\daemons.ps1              register (or re-register) every task below and start them
#   powershell -File win\daemons.ps1 -Only web    only that one
#
# Stop one:   .venv\Scripts\python.exe win\keep_running.py stop web   (the 5-minute watchdog starts it again unless
#             the task is disabled first: Disable-ScheduledTask "VOD web")
# Status:     .venv\Scripts\python.exe win\keep_running.py status
# Logs:       logs\<name>.out
param([string]$Only = "")
$ErrorActionPreference = "Stop"
$Hub = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$Py = "$Hub\.venv\Scripts\python.exe"
$Cloudflared = "$env:USERPROFILE\tools\cloudflared\cloudflared-windows-amd64.exe"

$Daemons = [ordered]@{
  # was vod-web.service: the public site (public.db, index/, features/ only), behind the tunnel
  "web"         = @{ env = "$Hub\site.env"; cmd = @($Py, "-m", "uvicorn", "web.app:app", "--host", "127.0.0.1", "--port", "8000",
                                                    "--proxy-headers", "--forwarded-allow-ips", "127.0.0.1") }
  # was vod-admin.service: the private admin app (archive.db), this PC only
  "admin"       = @{ env = ""; cmd = @($Py, "-m", "uvicorn", "admin.app:app", "--host", "127.0.0.1", "--port", "8001") }
  # was vod-chat-logger.service: 24/7 anonymous Twitch chat logger -> archive/archive.db (live_msgs, sessions)
  "chat-logger" = @{ env = "$Hub\.env"; cmd = @($Py, "pipeline\chat_logger.py") }
  # was cloudflared.service: the Cloudflare tunnel for thehasanproject.org (config: ~\.cloudflared\config.yml)
  "cloudflared" = @{ env = ""; cmd = @($Cloudflared, "--no-autoupdate", "--config", "$env:USERPROFILE\.cloudflared\config.yml", "tunnel", "run") }
}

function Quote([string]$s) { if ($s -match '[\s"]') { '"' + ($s -replace '"', '\"') + '"' } else { $s } }

$u = "$env:USERDOMAIN\$env:USERNAME"
foreach ($name in $Daemons.Keys) {
  if ($Only -and $name -ne $Only) { continue }
  $d = $Daemons[$name]
  $argv = @($Py, "$Hub\win\keep_running.py", "run", $name, "--cwd", $Hub, "--log", "$Hub\logs\$name.out")
  if ($d.env) { $argv += @("--env-file", $d.env) }
  $argv += @("--") + $d.cmd
  $a = New-ScheduledTaskAction -Execute "C:\Windows\System32\conhost.exe" -Argument ("--headless " + (($argv | % { Quote $_ }) -join " ")) -WorkingDirectory $Hub
  $t1 = New-ScheduledTaskTrigger -AtLogOn -User $u
  $t2 = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 5)
  $s = New-ScheduledTaskSettingsSet -ExecutionTimeLimit 0 -MultipleInstances IgnoreNew -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries -StartWhenAvailable -Priority 4
  $p = New-ScheduledTaskPrincipal -UserId $u -LogonType Interactive -RunLevel Limited
  Register-ScheduledTask -TaskName "VOD $name" -Action $a -Trigger $t1, $t2 -Settings $s -Principal $p -Force | Out-Null
  Start-ScheduledTask -TaskName "VOD $name"
  "registered and started: VOD $name"
}
