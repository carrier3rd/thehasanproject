#!/usr/bin/env bash
# GPU thermostat (owner's request 2026-11-11: keep the new laptop at normal temperatures, not flat out).
# Every 10 s: if the GPU is at or above HOT, pause (SIGSTOP) every transcription process; resume (SIGCONT) once it
# has cooled to COOL. No admin rights needed. On exit it always resumes anything it paused.
HOT=${HOT:-75}; COOL=${COOL:-68}
LOG=~/vod-archive/logs/gpu-thermostat.log
paused=0
pids() { pgrep -f "pipeline/[p]rocess.py"; }
resume() { [ "$paused" = 1 ] && { for p in $(pids); do kill -CONT "$p" 2>/dev/null; done; echo "$(date '+%F %T') resumed (exit)" >> "$LOG"; }; }
trap 'resume; exit' INT TERM EXIT
echo "$(date '+%F %T') thermostat on: pause at ${HOT}C, resume at ${COOL}C" >> "$LOG"
while true; do
  t=$(nvidia-smi --query-gpu=temperature.gpu --format=csv,noheader,nounits 2>/dev/null | head -1)
  if [ -n "$t" ]; then
    if [ "$paused" = 0 ] && [ "$t" -ge "$HOT" ] && [ -n "$(pids)" ]; then
      for p in $(pids); do kill -STOP "$p" 2>/dev/null; done; paused=1
      echo "$(date '+%F %T') ${t}C: paused transcription" >> "$LOG"
    elif [ "$paused" = 1 ] && [ "$t" -le "$COOL" ]; then
      for p in $(pids); do kill -CONT "$p" 2>/dev/null; done; paused=0
      echo "$(date '+%F %T') ${t}C: resumed" >> "$LOG"
    fi
    date "+%F %T ${t}C paused=$paused" > ~/vod-archive/logs/gpu-temp-now.txt
  fi
  sleep 10
done
