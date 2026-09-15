#!/bin/sh
# Glimpsely launchd entry: ensure local model server is healthy, then run bot.
set -u
OMLX_BIN="/opt/homebrew/bin/omlx"
HEALTH_URL="http://127.0.0.1:8000/v1/models"
HEALTH_HDR="Authorization: Bearer 1234"

check_health() {
  /usr/bin/curl -sf -o /dev/null -H "$HEALTH_HDR" "$HEALTH_URL"
}

if ! check_health; then
  echo "$(date '+%F %T') omlx not healthy, starting..."
  "$OMLX_BIN" start >/dev/null 2>&1 || true
  i=0
  while [ "$i" -lt 12 ]; do
    sleep 5
    if check_health; then
      echo "$(date '+%F %T') omlx healthy"
      break
    fi
    i=$((i + 1))
  done
fi

if ! check_health; then
  echo "$(date '+%F %T') WARNING: omlx still unhealthy; bot will degrade on LLM calls"
fi

exec /Users/alex/coding/Glimpsely/.venv/bin/python -m glimpsely.main run
