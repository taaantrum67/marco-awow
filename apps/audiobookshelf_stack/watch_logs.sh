#!/usr/bin/env bash
set -Eeuo pipefail
shopt -s nullglob

WEBHOOK_URL="https://discord.com/api/webhooks/1419786483677790359/-vGb-7sM1exHJne6pMKTTttNFzKnQvV1Ir0sRJK-_tk33fWtRgt6UAZW6JunFu7L2plU"

# ----------------------------
# Pfade
# ----------------------------
LOG_DIR="/metadata/logs/daily"
STATE_DIR="/config/webhook_script"
SEEN_FILE="$STATE_DIR/.seen_books"

mkdir -p "$STATE_DIR"
touch "$SEEN_FILE"

# ----------------------------
# Hilfsfunktionen
# ----------------------------
json_escape() {
  local s="$1"
  s="${s//\\/\\\\}"    # backslash
  s="${s//\"/\\\"}"    # quotes
  s="${s//$'\n'/\\n}"  # newlines
  s="${s//$'\r'/}"     # drop CR
  printf '%s' "$s"
}

send_discord_notification() {
  local message="$1"
  local esc
  esc="$(json_escape "$message")"
  curl -s -H "Content-Type: application/json" \
       -X POST \
       -d "{\"content\":\"$esc\"}" \
       "$WEBHOOK_URL" >/dev/null || true
  echo ">>> SENT TO DISCORD: $message"
}

# ----------------------------
# Haupt-Logik
# ----------------------------
process_line() {
  local line="$1"

  # Jede neue Zeile ins Container-Log
  echo "NEW LOG LINE: $line"

  # JSON -> message Feld extrahieren
  local msg
  msg=$(printf '%s' "$line" | jq -r '.message' 2>/dev/null || true)
  echo "DEBUG MESSAGE: $msg"
  [[ -z "$msg" ]] && return 0

  # Regex-Match auf "Created new library item"
  if [[ $msg =~ Created[[:space:]]new[[:space:]]library[[:space:]]item ]]; then
    echo "!!! MATCH FOUND in message: $msg"

    # Titel aus [Scan] "…"
    local book=""
    if [[ $msg =~ \[Scan\]\ \"([^\"]+)\" ]]; then
      book="${BASH_REMATCH[1]}"
      echo "BOOK EXTRACTED: $book"
    else
      echo "MATCH but could not extract book title"
      return 0
    fi

    # Hash der gesamten Zeile
    local id_hash
    id_hash="$(printf '%s' "$line" | md5sum | awk '{print $1}')"

    if grep -q "$id_hash" "$SEEN_FILE"; then
      echo "HASH CHECK: already known ($id_hash)"
      return 0
    else
      echo "HASH CHECK: new ($id_hash)"
      echo "$id_hash" >> "$SEEN_FILE"
    fi

    # Discord senden
    send_discord_notification "📘 New audiobook imported: $book"
  fi
}

# ----------------------------
# Log-Follow
# ----------------------------
echo "Starting audiobook log watcher..."
echo "Watching logs in $LOG_DIR"

while true; do
  current=$(ls -1t "$LOG_DIR"/*.txt 2>/dev/null | head -n1)
  if [[ -z "$current" ]]; then
    sleep 2
    continue
  fi

  echo "Following $current"
  stdbuf -oL -eL tail -n0 -F "$current" 2>/dev/null | \
  while IFS= read -r line; do
    process_line "$line"
  done

  sleep 1
done
