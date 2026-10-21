#!/usr/bin/env bash
# Restart the watcher within 5 s if it dies (use systemd instead when available).
PROFILE="${1:-configs/profiles/gpu-workstation.toml}"
cd "$(dirname "$0")/.." || exit 1
while true; do
  if ! pgrep -f "guardrails.*watch" >/dev/null 2>&1; then
    echo "$(date -u +%FT%TZ) watcher not running: starting" >> "${GUARDRAILS_LOG:-/tmp/guardrails-supervisor.log}"
    setsid nohup python3 -m guardrails -c "$PROFILE" watch >/dev/null 2>&1 < /dev/null &
  fi
  sleep 5
done
