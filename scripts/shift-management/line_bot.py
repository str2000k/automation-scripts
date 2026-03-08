#!/usr/bin/env python3
"""STEP 4: LINE Bot for shift preference collection.

Features:
4-1. Webhook endpoint for LINE Messaging API
4-2. broadcast_shift_collect() - broadcast collection message to all followers
4-3. handle_message() - parse shift preferences and register to kintone

Usage:
    python3 line_bot.py serve         # Start webhook server (port 8443)
    python3 line_bot.py broadcast     # Manually trigger broadcast

Webhook URL: https://<your-domain>/callback
"""
import hashlib
import hmac
import base64
import json
import re
import sys
import urllib.request
import urllib.error
import urllib.parse
from datetime import datetime, timedelta
from http.server import HTTPServer, BaseHTTPRequestHandler

from config import (
    KINTONE_DOMAIN, KINTONE_USERNAME, KINTONE_PASSWORD,
    KINTONE_SHIFT_WISH_APP_ID,
    LINE_CHANNEL_ACCESS_TOKEN, LINE_CHANNEL_SECRET,
    SLACK_BOT_TOKEN, SLACK_SHIFT_CHANNEL,
)


# ---------------------------------------------------------------------------
# API helpers
# ---------------------------------------------------------------------------

def line_api(path, body):
    """Call LINE Messaging API."""
    url = f"https://api.line.me/v2/bot/{path}"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}",
    }
    data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, headers=headers)
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read())


def line_reply(reply_token, text):
    """Reply to a LINE message."""
    line_api("message/reply", {
        "replyToken": reply_token,
        "messages": [{"type": "text", "text": text}],
    })


def line_broadcast(text):
    """Broadcast message to all followers."""
    line_api("message/broadcast", {
        "messages": [{"type": "text", "text": text}],
    })


def kintone_api(path, method="GET", body=None):
    url = f"https://{KINTONE_DOMAIN}/k/v1/{path}"
    credential = base64.b64encode(
        f"{KINTONE_USERNAME}:{KINTONE_PASSWORD}".encode()
    ).decode()
    headers = {"X-Cybozu-Authorization": credential}
    if body:
        headers["Content-Type"] = "application/json"
        data = json.dumps(body).encode()
    else:
        data = None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read())


def slack_notify_admin(text):
    """Send error notification to Slack shift channel."""
    if not SLACK_BOT_TOKEN:
        print(f"[Slack skip] {text}")
        return
    url = "https://slack.com/api/chat.postMessage"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {SLACK_BOT_TOKEN}",
    }
    body = {
        "channel": SLACK_SHIFT_CHANNEL,
        "text": f"[LINE Bot Error] {text}",
    }
    data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, headers=headers)
    try:
        with urllib.request.urlopen(req) as resp:
            resp.read()
    except Exception as e:
        print(f"Slack notify failed: {e}")


def get_line_user_name(user_id):
    """Get LINE user display name via profile API."""
    url = f"https://api.line.me/v2/bot/profile/{user_id}"
    headers = {"Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}"}
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req) as resp:
            profile = json.loads(resp.read())
            return profile.get("displayName", user_id)
    except Exception:
        return user_id


# ---------------------------------------------------------------------------
# Signature verification
# ---------------------------------------------------------------------------

def verify_signature(body_bytes, signature):
    """Verify LINE webhook signature."""
    if not LINE_CHANNEL_SECRET:
        return True  # Skip verification if secret not configured
    mac = hmac.HMAC(
        LINE_CHANNEL_SECRET.encode(), body_bytes, hashlib.sha256
    )
    expected = base64.b64encode(mac.digest()).decode()
    return hmac.compare_digest(signature, expected)


# ---------------------------------------------------------------------------
# 4-2. Broadcast shift collection message
# ---------------------------------------------------------------------------

def get_next_shift_period():
    """Calculate next 2-week shift period (same logic as slack_shift_bot)."""
    today = datetime.now()
    days_ahead = 7 - today.weekday()
    if days_ahead <= 0:
        days_ahead += 7
    start = today + timedelta(days=days_ahead)
    end = start + timedelta(days=13)
    return start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")


def broadcast_shift_collect():
    """Broadcast shift preference collection message to all LINE followers."""
    if not LINE_CHANNEL_ACCESS_TOKEN:
        print("[LINE broadcast skip] No LINE_CHANNEL_ACCESS_TOKEN configured.")
        return

    period_start, period_end = get_next_shift_period()
    deadline = (datetime.now() + timedelta(days=5)).strftime("%Y-%m-%d")

    text = (
        "シフト希望収集のお知らせ\n\n"
        f"対象期間: {period_start} ~ {period_end}\n"
        f"回答期限: {deadline}\n\n"
        "以下の形式でこのLINEに返信してください:\n"
        "希望休 4/5, 4/12\n\n"
        "複数日ある場合はカンマ区切りで送信してください。\n"
        "備考がある場合は2行目に記入:\n"
        "希望休 4/5, 4/12\n"
        "備考 午前のみ希望(4/8)"
    )

    try:
        line_broadcast(text)
        print(f"LINE broadcast sent: {period_start} ~ {period_end}")
    except Exception as e:
        print(f"LINE broadcast error: {e}")
        slack_notify_admin(f"LINE broadcast failed: {e}")


# ---------------------------------------------------------------------------
# 4-3. Handle incoming messages
# ---------------------------------------------------------------------------

def normalize_date(raw, reference_year=None):
    """Normalize date string like '4/5' or '2026-04-05' to 'YYYY-MM-DD'."""
    if reference_year is None:
        reference_year = datetime.now().year

    raw = raw.strip().replace(" ", "")

    # Already YYYY-MM-DD
    m = re.match(r"(\d{4})-(\d{1,2})-(\d{1,2})$", raw)
    if m:
        return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"

    # M/D format
    m = re.match(r"(\d{1,2})/(\d{1,2})$", raw)
    if m:
        month, day = int(m.group(1)), int(m.group(2))
        # If month is past, assume next year
        now = datetime.now()
        year = reference_year
        if month < now.month or (month == now.month and day < now.day):
            year += 1
        return f"{year}-{month:02d}-{day:02d}"

    return raw  # Return as-is if unparseable


def parse_line_message(text):
    """Parse LINE message for shift preferences.

    Expected format: '希望休 4/5, 4/12'
    Optional second line: '備考 午前のみ希望(4/8)'

    Returns (days_off_str, remarks) or (None, None) if not a shift message.
    """
    lines = text.strip().split("\n")
    days_off = None
    remarks = ""

    for line in lines:
        line = line.strip()
        # Match: 希望休 dates
        m = re.match(r"希望休[：:\s]*(.+)", line)
        if m:
            raw_dates = m.group(1).strip()
            parts = re.split(r"[,、\s]+", raw_dates)
            normalized = [normalize_date(d) for d in parts if d]
            days_off = ", ".join(normalized)
            continue

        # Match: 備考
        m = re.match(r"備考[：:\s]*(.+)", line)
        if m:
            remarks = m.group(1).strip()
            continue

    return days_off, remarks


def handle_message(event):
    """Process a LINE text message event.

    Parses shift preferences and registers to kintone app 211.
    """
    reply_token = event["replyToken"]
    user_id = event["source"]["userId"]
    text = event["message"]["text"]

    # Only process messages that look like shift preferences
    days_off, remarks = parse_line_message(text)
    if days_off is None:
        # Not a shift preference message - ignore silently
        return

    user_name = get_line_user_name(user_id)
    period_start, period_end = get_next_shift_period()
    now = datetime.now().strftime("%Y-%m-%dT%H:%M:%S+09:00")

    record = {
        "staff_name": {"value": user_name},
        "target_period_start": {"value": period_start},
        "target_period_end": {"value": period_end},
        "desired_days_off": {"value": days_off},
        "remarks": {"value": remarks},
        "input_channel": {"value": "LINE"},
        "submitted_at": {"value": now},
        "status": {"value": "未処理"},
    }

    try:
        kintone_api("record.json", "POST", {
            "app": KINTONE_SHIFT_WISH_APP_ID,
            "record": record,
        })
        line_reply(reply_token, "希望を受け付けました")
        print(f"[LINE] Registered: {user_name} off={days_off}")
    except Exception as e:
        error_msg = f"LINE user={user_name} ({user_id}), text={text}, error={e}"
        print(f"[LINE Error] {error_msg}")
        line_reply(reply_token, "登録中にエラーが発生しました。管理者に連絡してください。")
        slack_notify_admin(error_msg)


# ---------------------------------------------------------------------------
# 4-1. Webhook HTTP server
# ---------------------------------------------------------------------------

class LineWebhookHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        if self.path != "/callback":
            self.send_response(404)
            self.end_headers()
            return

        content_length = int(self.headers.get("Content-Length", 0))
        body_bytes = self.rfile.read(content_length)

        # Verify signature
        signature = self.headers.get("X-Line-Signature", "")
        if not verify_signature(body_bytes, signature):
            print("[LINE Webhook] Invalid signature")
            self.send_response(403)
            self.end_headers()
            return

        # Process events
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{}')

        try:
            payload = json.loads(body_bytes)
            for event in payload.get("events", []):
                if event["type"] == "message" and event["message"]["type"] == "text":
                    handle_message(event)
        except Exception as e:
            print(f"[LINE Webhook] Error processing event: {e}")
            slack_notify_admin(f"Webhook processing error: {e}")

    def do_GET(self):
        """Health check endpoint."""
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"LINE Bot is running")

    def log_message(self, format, *args):
        print(f"[LINE Webhook] {args[0]}")


def serve(port=8443):
    """Start webhook server."""
    server = HTTPServer(("0.0.0.0", port), LineWebhookHandler)
    print(f"LINE Bot webhook server starting on port {port}")
    print(f"Webhook URL: https://<your-domain>:{port}/callback")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down...")
        server.server_close()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    if len(sys.argv) < 2:
        print("Usage: python3 line_bot.py <command>")
        print("Commands: serve, broadcast")
        sys.exit(1)

    cmd = sys.argv[1]
    if cmd == "serve":
        port = int(sys.argv[2]) if len(sys.argv) > 2 else 8443
        serve(port)
    elif cmd == "broadcast":
        broadcast_shift_collect()
    else:
        print(f"Unknown command: {cmd}")
        sys.exit(1)


if __name__ == "__main__":
    main()
