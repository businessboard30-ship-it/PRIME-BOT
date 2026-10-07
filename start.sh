#!/bin/sh
# Picks the right start command for each Railway service of this repo.
# Optional override: set SERVICE_ROLE=bot|api|clone|dashboard on a service.

role="$SERVICE_ROLE"
if [ -z "$role" ]; then
  name="$(printf '%s' "$RAILWAY_SERVICE_NAME" | tr '[:upper:]' '[:lower:]')"
  case "$name" in
    *clone*)           role=clone ;;
    *dashboard*)       role=dashboard ;;
    *api*|*web*)       role=api ;;
    *bot*)             role=bot ;;
    *)                 role=api ;;  # fallback: unknown name -> API server
  esac
fi

echo "[start.sh] service='$RAILWAY_SERVICE_NAME' role='$role'"

case "$role" in
  bot)       exec python -m discord_bot.bot ;;
  clone)     exec python -u -m discord_bot.clone_manager ;;
  dashboard) exec pnpm start ;;
  api|*)     exec python -u api_server.py ;;
esac
