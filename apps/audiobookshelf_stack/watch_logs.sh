#!/bin/bash

# ----------------------------
# Discord Webhook URL
# ----------------------------
WEBHOOK_URL="https://discord.com/api/webhooks/1419786483677790359/-vGb-7sM1exHJne6pMKTTttNFzKnQvV1Ir0sRJK-_tk33fWtRgt6UAZW6JunFu7L2plU"

send_discord_notification() {
    local message="$1"
    json_message=$(printf '%s' "$message" | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read()))')
    curl -s -H "Content-Type: application/json" \
         -X POST \
         -d "{\"content\": $json_message}" \
         "$WEBHOOK_URL"
}

# ----------------------------
# Log directory and trigger log inside container
# ----------------------------
LOG_DIR="/metadata/logs/daily"
SEEN_FILE="/config/webhook_script/.seen_books"
TRIGGER_LOG="/config/webhook_script/watch_trigger.log"

mkdir -p "$(dirname "$SEEN_FILE")"
touch "$SEEN_FILE"
touch "$TRIGGER_LOG"

echo "Starting audiobook log watcher..."
echo "Watching logs in $LOG_DIR"

# ----------------------------
# Watch log files
# ----------------------------
tail -F "$LOG_DIR"/*.txt | while read -r line; do
    if [[ "$line" =~ Created\ new\ library\ item ]]; then
        id_hash=$(echo -n "$line" | md5sum | cut -d' ' -f1)

        if ! grep -q "$id_hash" "$SEEN_FILE"; then
            # Extract only book name
            book=$(echo "$line" | sed -E 's/.*Created new library item "([^"]+)".*/\1/')

            if [[ -n "$book" ]]; then
                echo "$(date '+%Y-%m-%d %H:%M:%S') - Trigger detected: $book"
                echo "$(date '+%Y-%m-%d %H:%M:%S') $book" >> "$TRIGGER_LOG"
                send_discord_notification "📘 New audiobook imported: $book"
                echo "$id_hash" >> "$SEEN_FILE"
                echo "$(date '+%Y-%m-%d %H:%M:%S') - Notification sent for: $book"
            fi
        fi
fi

done
