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
# Paths
# ----------------------------
LOG_DIR="/metadata/logs/daily"
SEEN_FILE="/config/webhook_script/.seen_books"
TRIGGER_LOG="/config/webhook_script/watch_trigger.log"
DEBUG_LOG="/config/webhook_script/watch_debug.log"

mkdir -p "$(dirname "$SEEN_FILE")"
touch "$SEEN_FILE"
touch "$TRIGGER_LOG"
touch "$DEBUG_LOG"

echo "$(date '+%Y-%m-%d %H:%M:%S') - Starting audiobook log watcher..." | tee -a "$DEBUG_LOG"
echo "$(date '+%Y-%m-%d %H:%M:%S') - Watching logs in $LOG_DIR" | tee -a "$DEBUG_LOG"

# ----------------------------
# Check if log directory exists
# ----------------------------
if [ ! -d "$LOG_DIR" ]; then
    echo "$(date '+%Y-%m-%d %H:%M:%S') - ERROR: Log directory $LOG_DIR does not exist!" | tee -a "$DEBUG_LOG"
    exit 1
fi

# ----------------------------
# Watch log files
# ----------------------------
tail -F "$LOG_DIR"/*.txt 2>>"$DEBUG_LOG" | while read -r line; do
    echo "$(date '+%Y-%m-%d %H:%M:%S') - DEBUG LINE: $line" | tee -a "$DEBUG_LOG"

    if echo "$line" | grep -q 'Created new library item'; then
        id_hash=$(echo -n "$line" | md5sum | cut -d' ' -f1)
        echo "$(date '+%Y-%m-%d %H:%M:%S') - Computed id_hash: $id_hash" | tee -a "$DEBUG_LOG"

        if ! grep -q "$id_hash" "$SEEN_FILE"; then
            book=$(echo "$line" | sed -E 's/.*Created new library item "(.*)".*/\1/')
            echo "$(date '+%Y-%m-%d %H:%M:%S') - Parsed book: $book" | tee -a "$DEBUG_LOG"

            if [ -n "$book" ]; then
                echo "$(date '+%Y-%m-%d %H:%M:%S') - Trigger detected: $book" | tee -a "$DEBUG_LOG"

                # Write to trigger log
                echo "$(date '+%Y-%m-%d %H:%M:%S') $book" >> "$TRIGGER_LOG" 2>>"$DEBUG_LOG"

                # Send notification
                send_discord_notification "📘 New audiobook imported: $book" 2>>"$DEBUG_LOG"

                # Mark as seen
                echo "$id_hash" >> "$SEEN_FILE" 2>>"$DEBUG_LOG"

                echo "$(date '+%Y-%m-%d %H:%M:%S') - Notification sent for: $book" | tee -a "$DEBUG_LOG"
            else
                echo "$(date '+%Y-%m-%d %H:%M:%S') - WARNING: Failed to parse book name from line" | tee -a "$DEBUG_LOG"
            fi
        else
            echo "$(date '+%Y-%m-%d %H:%M:%S') - Already processed id_hash: $id_hash" | tee -a "$DEBUG_LOG"
        fi
    else
        echo "$(date '+%Y-%m-%d %H:%M:%S') - No trigger pattern matched for line" | tee -a "$DEBUG_LOG"
    fi
done
