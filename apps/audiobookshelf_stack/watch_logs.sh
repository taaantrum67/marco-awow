#!/bin/bash

WEBHOOK_URL="https://discord.com/api/webhooks/1419786483677790359/-vGb-7sM1exHJne6pMKTTttNFzKnQvV1Ir0sRJK-_tk33fWtRgt6UAZW6JunFu7L2plU"

send_discord_notification() {
    local message="$1"
    curl -s -H "Content-Type: application/json" \
         -X POST \
         -d "{\"content\": \"${message//\"/\\\"}\"}" \
         "$WEBHOOK_URL" >/dev/null
}

LOG_DIR="/metadata/logs/daily"
SEEN_FILE="/config/webhook_script/.seen_books"
TRIGGER_LOG="/config/webhook_script/watch_trigger.log"

mkdir -p "$(dirname "$SEEN_FILE")"
touch "$SEEN_FILE" "$TRIGGER_LOG"

echo "Starting audiobook log watcher..."
echo "Watching logs in $LOG_DIR"

tail -F "$LOG_DIR"/*.txt | while read -r line; do
    # Nur reagieren, wenn die Zeile das Ereignis enthält
    [[ "$line" != *"Created new library item"* ]] && continue

    # Titel robust via Bash-Regex aus [Scan] "…"
    book=""
    if [[ "$line" =~ \[Scan\]\ \"([^\"]+)\" ]]; then
        book="${BASH_REMATCH[1]}"
    fi

    # Wenn kein Treffer, nichts senden (verhindert Senden der ganzen JSON-Zeile)
    [[ -z "$book" ]] && continue

    id_hash=$(printf '%s' "$line" | md5sum | awk '{print $1}')
    if ! grep -q "$id_hash" "$SEEN_FILE"; then
        ts="$(date '+%Y-%m-%d %H:%M:%S')"
        echo "$ts - Trigger detected: $book"
        echo "$ts $book" >> "$TRIGGER_LOG"
        send_discord_notification "📘 New audiobook imported: $book"
        echo "$id_hash" >> "$SEEN_FILE"
        echo "$ts - Notification sent for: $book"
    fi
done
