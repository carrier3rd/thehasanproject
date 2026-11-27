# Always-on VOD archive programs (moved off the Ubuntu laptop on 2026-11-26). Each runs under keep_running.py
# (restart on exit, one copy, no idle sleep) from a scheduled task that starts it at logon (desktop) or at boot
# (-Server) and re-launches it every 5 minutes if it died (a running copy makes the re-launch exit at once).
#
#   powershell -File win\daemons.ps1                 register (or re-register) every task below and start them
#   powershell -File win\daemons.ps1 -Only web       only that one
#   powershell -File win\daemons.ps1 -Server         a server: run at boot without anyone logged in (tasklib.ps1)
#   powershell -File win\daemons.ps1 -NoStart        register only
#   powershell -File win\daemons.ps1 -Disable        switch them off here (stop each with keep_running.py stop NAME)
#
# Status:     .venv\Scripts\python.exe win\keep_running.py status
# Logs:       logs\<name>.out
param([string]$Only = "", [switch]$Server, [switch]$NoStart, [switch]$Disable)
$ErrorActionPreference = "Stop"
. "$PSScriptRoot\tasklib.ps1"
$Hub = Split-Path -Parent $PSScriptRoot
$Py = "$Hub\.venv\Scripts\python.exe"
$Cloudflared = @("$env:USERPROFILE\tools\cloudflared\cloudflared.exe", "$env:USERPROFILE\tools\cloudflared\cloudflared-windows-amd64.exe") |
               ? { Test-Path $_ } | Select-Object -First 1

$Daemons = [ordered]@{
  # the public site (public.db, index/, features/ only), behind the tunnel
  "web"         = @{ env = "$Hub\site.env"; cmd = @($Py, "-m", "uvicorn", "web.app:app", "--host", "127.0.0.1", "--port", "8000",
                                                    "--proxy-headers", "--forwarded-allow-ips", "127.0.0.1") }
  # the private admin app (archive.db), this machine only
  "admin"       = @{ env = ""; cmd = @($Py, "-m", "uvicorn", "admin.app:app", "--host", "127.0.0.1", "--port", "8001") }
  # 24/7 anonymous Twitch chat logger -> archive/archive.db (live_msgs, sessions)
  "chat-logger" = @{ env = "$Hub\.env"; cmd = @($Py, "pipeline\chat_logger.py") }
  # the Cloudflare tunnel for the public site (config: ~\.cloudflared\config.yml)
  "cloudflared" = @{ env = ""; cmd = @($Cloudflared, "--no-autoupdate", "--config", "$env:USERPROFILE\.cloudflared\config.yml", "tunnel", "run") }
}

foreach ($name in $Daemons.Keys) {
  if ($Only -and $name -ne $Only) { continue }
  if ($Disable) {
    if (Get-ScheduledTask -TaskName "VOD $name" -ErrorAction SilentlyContinue) { Disable-ScheduledTask -TaskName "VOD $name" | Out-Null; "disabled: VOD $name" }
    continue
  }
  $d = $Daemons[$name]
  $argv = @($Py, "$Hub\win\keep_running.py", "run", $name, "--cwd", $Hub, "--log", "$Hub\logs\$name.out")
  if ($d.env) { $argv += @("--env-file", $d.env) }
  $argv += @("--") + $d.cmd
  Register-Job -Name "VOD $name" -Argv $argv -WorkDir $Hub -Daemon -Priority 4 -Server:$Server
  if (-not $NoStart) { Start-ScheduledTask -TaskName "VOD $name"; "  started" }
}
