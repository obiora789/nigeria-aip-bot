#!/usr/bin/env bash
# set_webhook_secret.sh — register Vannie's webhook with Telegram WITH a secret
# token, so Telegram sends it on every update (X-Telegram-Bot-Api-Secret-Token)
# and main.py can refuse anything that does not carry it.
#
# Run ONCE, with the SAME secret you put in Render as TELEGRAM_WEBHOOK_SECRET:
#
#   export TELEGRAM_BOT_TOKEN='...'            # from BotFather (never commit it)
#   export TELEGRAM_WEBHOOK_SECRET='...'       # e.g. from: openssl rand -hex 32
#   bash set_webhook_secret.sh
#
# Telegram allows A-Z a-z 0-9 _ - in the secret, 1-256 characters.
set -euo pipefail
: "${TELEGRAM_BOT_TOKEN:?export TELEGRAM_BOT_TOKEN first}"
: "${TELEGRAM_WEBHOOK_SECRET:?export TELEGRAM_WEBHOOK_SECRET first}"
URL="${WEBHOOK_URL:-https://nigeria-aip-bot.onrender.com/webhook}"
# Length is checked separately: macOS's regex library rejects a {1,256}
# repeat count (its limit is 255), so the old one-line pattern refused every
# secret on a Mac, valid or not (found 2026-11-25).
if (( ${#TELEGRAM_WEBHOOK_SECRET} < 1 || ${#TELEGRAM_WEBHOOK_SECRET} > 256 )) ||
   ! [[ "$TELEGRAM_WEBHOOK_SECRET" =~ ^[A-Za-z0-9_-]+$ ]]; then
  echo "secret must be 1-256 characters of A-Z a-z 0-9 _ -" >&2; exit 1
fi
echo "Registering $URL with a secret token..."
curl -fsS "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/setWebhook" \
     --data-urlencode "url=${URL}" \
     --data-urlencode "secret_token=${TELEGRAM_WEBHOOK_SECRET}"
echo
echo "Telegram now reports:"
curl -fsS "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/getWebhookInfo"
echo
