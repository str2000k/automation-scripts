#!/usr/bin/env python3
"""
bolt_server.py

Slack Bolt server for shift button interactions.
Receives button taps from staff and updates kintone ID=211.

Usage:
  python3 bolt_server.py

Production: expose via ngrok or fixed server.
  ngrok http 3000
  -> Set Request URL in Slack App > Interactivity & Shortcuts
"""

import base64
import datetime
import json
import logging
import re
import urllib.parse
import urllib.request

from slack_bolt import App
from slack_bolt.adapter.flask import SlackRequestHandler
from flask import Flask, request

from config import (
    SLACK_BOT_TOKEN,
    SLACK_SIGNING_SECRET,
    BOLT_PORT,
    KINTONE_DOMAIN,
    KINTONE_USERNAME,
    KINTONE_PASSWORD,
    KINTONE_SHIFT_WISH_APP_ID,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Bolt app
bolt_app = App(
    token=SLACK_BOT_TOKEN,
    signing_secret=SLACK_SIGNING_SECRET,
)

flask_app = Flask(__name__)
handler = SlackRequestHandler(bolt_app)

SHIFT_CHANNEL = "C0AKBJ1LTV2"  # #shift-management


# -----------------------------------------------------------------------
# kintone helpers (same auth as rest of codebase)
# -----------------------------------------------------------------------

def _kintone_headers():
    credential = base64.b64encode(
        f"{KINTONE_USERNAME}:{KINTONE_PASSWORD}".encode()
    ).decode()
    return {
        "X-Cybozu-Authorization": credential,
        "Content-Type": "application/json",
    }


def update_kintone_shift(staff_id: str, shift_date: str, shift_status: str) -> bool:
    """Update kintone ID=211 record (staff_id x shift_date)."""
    headers = _kintone_headers()

    # Search for record
    query = f'staff_id = "{staff_id}" and shift_date = "{shift_date}"'
    search_body = {
        "app": KINTONE_SHIFT_WISH_APP_ID,
        "query": query,
        "fields": ["$id"],
    }
    url = f"https://{KINTONE_DOMAIN}/k/v1/records.json"
    req = urllib.request.Request(
        url,
        data=json.dumps(search_body).encode(),
        headers=headers,
        method="GET",
    )
    try:
        with urllib.request.urlopen(req) as resp:
            data = json.loads(resp.read())
    except Exception as e:
        logger.error(f"kintone search failed: {e}")
        return False

    records = data.get("records", [])
    if not records:
        logger.error(f"Record not found: {staff_id} / {shift_date}")
        return False

    record_id = records[0]["$id"]["value"]

    # Update record
    update_url = f"https://{KINTONE_DOMAIN}/k/v1/record.json"
    payload = {
        "app": KINTONE_SHIFT_WISH_APP_ID,
        "id": record_id,
        "record": {
            "shift_type": {"value": shift_status},
            "input_status": {"value": "入力済"},
            "submitted_at": {"value": datetime.datetime.now().isoformat()},
        },
    }
    req = urllib.request.Request(
        update_url,
        data=json.dumps(payload).encode(),
        headers=headers,
        method="PUT",
    )
    try:
        with urllib.request.urlopen(req) as resp:
            json.loads(resp.read())
    except Exception as e:
        logger.error(f"kintone update failed: {e}")
        return False

    logger.info(f"kintone updated: {staff_id} / {shift_date} -> {shift_status}")
    return True


def build_confirmed_block(date_str: str, weekday: str, shift_status: str, staff_name: str) -> dict:
    """Return a block showing the selected status (buttons replaced with text)."""
    emoji = {"出勤": "✅", "休み": "😴", "希望休": "🙏"}.get(shift_status, "")
    return {
        "type": "section",
        "block_id": f"shift_{date_str}",
        "text": {
            "type": "mrkdwn",
            "text": f"*{date_str}（{weekday}）*　→　{emoji} *{shift_status}*",
        },
    }


# -----------------------------------------------------------------------
# Bolt action handler
# action_id pattern: "shift_YYYY-MM-DD_出勤" / "shift_YYYY-MM-DD_休み" / "shift_YYYY-MM-DD_希望休"
# -----------------------------------------------------------------------

@bolt_app.action(re.compile(r"^shift_\d{4}-\d{2}-\d{2}_.+$"))
def handle_shift_action(ack, body, client, logger):
    """Handle staff tapping a shift button."""
    ack()

    action = body["actions"][0]
    action_id = action["action_id"]
    user_id = body["user"]["id"]
    user_name = body["user"].get("name", user_id)

    # Parse action_id: "shift_2026-03-09_出勤"
    parts = action_id.split("_", 2)  # ["shift", "2026-03-09", "出勤"]
    if len(parts) != 3:
        logger.error(f"Invalid action_id: {action_id}")
        return

    shift_date = parts[1]
    shift_status = parts[2]

    # staff_id from button value
    staff_id = action.get("value", "")

    weekdays = ["月", "火", "水", "木", "金", "土", "日"]
    d = datetime.date.fromisoformat(shift_date)
    weekday = weekdays[d.weekday()]

    logger.info(f"Shift action: {staff_id} / {shift_date} / {shift_status} by {user_name}")

    # Update kintone
    success = update_kintone_shift(staff_id, shift_date, shift_status)

    if not success:
        client.chat_postMessage(
            channel=user_id,
            text=f"❌ kintone の更新に失敗しました。({shift_date} / {shift_status})\n管理者に連絡してください。",
        )
        return

    # Update message: replace the date's section+actions blocks with confirmed block
    original_blocks = body["message"]["blocks"]
    new_blocks = []
    skip_next_actions = False

    for block in original_blocks:
        block_id = block.get("block_id", "")

        if skip_next_actions:
            # Skip the actions block that follows the section we just replaced
            if block.get("type") == "actions" and block_id == f"actions_{shift_date}":
                skip_next_actions = False
                continue
            skip_next_actions = False

        if block_id == f"shift_{shift_date}":
            new_blocks.append(build_confirmed_block(shift_date, weekday, shift_status, user_name))
            skip_next_actions = True
        else:
            new_blocks.append(block)

    client.chat_update(
        channel=body["container"]["channel_id"],
        ts=body["container"]["message_ts"],
        blocks=new_blocks,
        text=f"シフト希望入力（{shift_date}: {shift_status}）",
    )

    # Notify admin channel
    client.chat_postMessage(
        channel=SHIFT_CHANNEL,
        text=f"📝 {user_name}（{staff_id}）が {shift_date}（{weekday}）のシフトを *{shift_status}* で入力しました。",
    )


# -----------------------------------------------------------------------
# Flask endpoints
# -----------------------------------------------------------------------

@flask_app.route("/slack/events", methods=["POST"])
def slack_events():
    return handler.handle(request)


@flask_app.route("/health", methods=["GET"])
def health():
    return "OK", 200


if __name__ == "__main__":
    logger.info(f"Starting Bolt server on port {BOLT_PORT}...")
    flask_app.run(host="0.0.0.0", port=BOLT_PORT)
