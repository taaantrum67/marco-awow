#!/bin/bash
set -Eeuo pipefail
shopt -s nullglob


WEBHOOK_URL="https://discord.com/api/webhooks/1419786483677790359/-vGb-7sM1exHJne6pMKTTttNFzKnQvV1Ir0sRJK-_tk33fWtRgt6UAZW6JunFu7L2plU"

# ----------------------------
# Pfade
# ----------------------------
LOG_DIR="/metadata/logs/daily"
STATE_DIR="/config/webhook_script"
SEEN_FILE="$STATE_DIR/.seen_books"
TRIGGER_LOG="$STATE_DIR/watch_trigger.log"

mkdir -p "$STATE_DIR"
: > /dev/null
touch "$SEEN_FILE" "$TRIGGER_LOG" || true

# ----------------------------
# Helfer
# ----------------------------
json_escape() {
  # escaped in variable ESC
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
}

process_line() {
  local line="$1"

  # Nur Zeilen mit dem Ereignis betrachten
  [[ "$line" != *"Created new library item"* ]] && return 0

  # Titel aus [Scan] "…"
  local book=""
  if [[ $line =~ \[Scan\]\ \"([^\"]+)\" ]]; then
    book="${BASH_REMATCH[1]}"
  else
    return 0
  fi

  # Dedupe per Hash der Ereigniszeile
  local id_hash
  id_hash="$(printf '%s' "$line" | md5sum | awk '{print $1}')"
  if grep -q "$id_hash" "$SEEN_FILE"; then
    return 0
  fi

  local ts
  ts="$(date '+%Y-%m-%d %H:%M:%S')"
  echo "$ts - Trigger detected: $book" | tee -a "$TRIGGER_LOG" >/dev/null
  send_discord_notification "📘 New audiobook imported: $book"
  echo "$id_hash" >> "$SEEN_FILE"
  echo "$ts - Notification sent for: $book" | tee -a "$TRIGGER_LOG" >/dev/null
}

# ----------------------------
# Log-Follow (robust bei Rotation und Start ohne Dateien)
# ----------------------------
echo "Starting audiobook log watcher..."
echo "Watching logs in $LOG_DIR"

while true; do
  files=( "$LOG_DIR"/*.txt )
  if (( ${#files[@]} == 0 )); then
    sleep 2
    continue
  fi

  # -F folgt Rotationen/Neuanlagen; stdbuf = zeilenpuffer
  stdbuf -oL -eL tail -n0 -F "${files[@]}" 2>/dev/null | \
  while IFS= read -r line; do
    process_line "$line"
  done

  # falls tail beendet wurde (z.B. kurzzeitig keine Dateien), neu versuchen
  sleep 1
done
