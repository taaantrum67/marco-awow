#!/bin/bash

# Define the Discord webhook URL
WEBHOOK_URL=${DISCORD_WEBHOOK}
# Function to send message to Discord
send_discord_notification() {
  local message=$1
  curl -H "Content-Type: application/json" \
       -X POST \
       -d "{\"content\": \"$message\"}" \
       $WEBHOOK_URL
}

# Monitor Docker logs for new book imports
docker logs -f audiobookshelf | while read -r line; do
  # Check for log entries indicating a new book import
  if [[ "$line" =~ "imported new book" ]]; then
    # Extract book title and author from the log line
    book_title=$(echo "$line" | grep -oP '(?<=title":")[^"]+')
    book_author=$(echo "$line" | grep -oP '(?<=author":")[^"]+')

    # Create the message
    message="📘 New audiobook imported: **$book_title** by $book_author"

    # Send the notification
    send_discord_notification "$message"
  fi
done