import os
import time

with open("/Users/manushivam/chanakya-bot/.env") as f:
    for line in f:
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ[k.strip()] = v.strip()

from slack_sdk import WebClient

client = WebClient(token=os.environ["SLACK_BOT_TOKEN"])

# Get bot's own user ID
bot_id = client.auth_test()["user_id"]

TARGET_CHANNELS = ["dalal-street-neeti", "wall-street-neeti", "crypto-kautilya", "realty-rajniti"]

# Resolve channel names to IDs
result = client.conversations_list(limit=200, types="public_channel")
channel_map = {c["name"]: c["id"] for c in result["channels"]}

for name in TARGET_CHANNELS:
    cid = channel_map.get(name)
    if not cid:
        print(f"Channel #{name} not found, skipping.")
        continue

    # Fetch recent messages (last 20)
    history = client.conversations_history(channel=cid, limit=20)
    deleted = 0
    for msg in history["messages"]:
        if msg.get("bot_id") or msg.get("user") == bot_id:
            try:
                client.chat_delete(channel=cid, ts=msg["ts"])
                deleted += 1
                time.sleep(0.5)  # rate limit
            except Exception as e:
                print(f"  Failed to delete in #{name}: {e}")

    print(f"#{name}: deleted {deleted} message(s)")
