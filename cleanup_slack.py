import os
import json
from urllib import request
from dotenv import load_dotenv

load_dotenv()

bot_token = os.getenv("SLACK_BOT_TOKEN")
channel_id = "C0A8M0VCT3L"

if not bot_token:
    print("SLACK_BOT_TOKEN not set in .env")
    exit(1)

headers = {"Authorization": f"Bearer {bot_token}", "Content-Type": "application/json"}

print(f"Fetching messages from channel {channel_id}...")
url = f"https://slack.com/api/conversations.history?channel={channel_id}&limit=100"
req = request.Request(url, headers=headers)
with request.urlopen(req) as response:
    data = json.loads(response.read().decode())
    print(f"API Response: {data}")
    
    if not data.get("ok"):
        print(f"Error: {data.get('error')}")
        exit(1)
    
    messages = data.get("messages", [])

if not messages:
    print("No messages found")
    exit(0)

print(f"Found {len(messages)} messages")

deleted_count = 0
for msg in messages:
    ts = msg.get("ts")
    text = msg.get("text", "")[:50]
    
    if "Solara ETL" in text or "Test message" in text or "ETL Pipeline" in text:
        print(f"Deleting: {text}...")
        delete_url = "https://slack.com/api/chat.delete"
        payload = json.dumps({"channel": channel_id, "ts": ts}).encode("utf-8")
        req = request.Request(delete_url, data=payload, headers=headers, method="POST")
        
        try:
            with request.urlopen(req) as response:
                result = json.loads(response.read().decode())
                if result.get("ok"):
                    deleted_count += 1
                    print(f"  Deleted!")
                else:
                    print(f"  Error: {result.get('error')}")
        except Exception as e:
            print(f"  Failed: {e}")

print(f"Deleted {deleted_count} messages")
