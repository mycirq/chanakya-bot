import os
import sys

# Load .env manually
with open("/Users/manushivam/chanakya-bot/.env") as f:
    for line in f:
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ[k.strip()] = v.strip()

from slack_bolt import App
from db import init_db
from scheduler import post_news

init_db()
app = App(token=os.environ["SLACK_BOT_TOKEN"])

slot = sys.argv[1] if len(sys.argv) > 1 else "morning"
print(f"Posting {slot} briefing to all channels...")
post_news(app, slot)
print("Done!")
