#!/bin/bash

# Define the Discord webhook URL
WEBHOOK_URL=${DISCORD_WEBHOOK}
# Function to send a message to Discord
send_discord_notification() {
  local message="$1"
  curl -H "Content-Type: application/json" \
       -X POST \
       -d "{\"content\": \"$message\"}" \
       $WEBHOOK_URL
}

# Monitor Audiobookshelf Docker logs continuously
docker logs -f audiobookshelf-server 2>&1 | while read -r line; do
  # Detect new library items
  if [[ "$line" =~ "Folder scan results" ]] || [[ "$line" =~ "Created new library item" ]]; then
    # Extract the book title from the log line
    if [[ "$line" =~ \"([^\"]+)\" ]]; then
      book="${BASH_REMATCH[1]}"
      # Optionally, split into author and title
      author=$(echo "$book" | cut -d'/' -f1)
      title=$(echo "$book" | cut -d'/' -f2-)
      
      # Send notification
      send_discord_notification "📘 New audiobook imported: **$title** by $author"
    fi
  fi
done