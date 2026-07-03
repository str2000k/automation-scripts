#!/usr/bin/env python3
"""STEP 4: LINE Bot for shift preference collection.

Features:
4-1. Webhook endpoint for LINE Messaging API
4-2. broadcast_shift_collect() - send collection message to 希望シフト staff
4-3. handle_message() - parse shift preferences and update kintone 211 records
4-4. send_line_approval_message() - notify individual staff of confirmed shifts
4-5. send_line_remind() - remind unsubmitted 希望シフト staff

Usage:
    python3 line_bot.py serve         # Start webhook server (port 8443)
    python3 line_bot.py broadcast     # Manually trigger broadcast
    python3 line_bot.py remind        # Send reminders to non-responders
    python3 line_bot.py approval      # Send approval notifications (requires args)

Webhook URL: https://<your-domain>/callback
"""
import hashlib
import hmac
import base64
import json
import os
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


def line_push(user_id, message):
    """Push message to a specific LINE user."""
    if not LINE_CHANNEL_ACCESS_TOKEN:
        print(f"  [LINE skip] No token. Would send to {user_id}: {message}")
        return
    line_api("message/push", {
        "to": user_id,
        "messages": [{"type": "text", "text": message}],
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
# Staff master helpers
# ---------------------------------------------------------------------------

def read_staff_master_from_cache():
    """Read staff master from staff_master_cache.json.

    Returns list of dicts with keys: staff_id, name, employment_type,
    stores, work_style, role, max_hours, forced_days, personal_rules,
    slack_id, line_uid, active
    """
    cache_path = os.path.join(os.path.dirname(__file__), "staff_master_cache.json")
    if os.path.exists(cache_path):
        with open(cache_path) as f:
            return json.load(f)
    print("WARNING: staff_master_cache.json not found.")
    print("Run 'python3 sync_staff_master.py' to fetch from Google Sheets.")
    return []


def get_shift_input_required_staff():
    """Get list of active staff who need to submit shift preferences.

    Filters for:
    - shift_type (働き方) == '希望シフト'
    - active (有効フラグ) == '有効' or True
    """
    staff = read_staff_master_from_cache()
    required = []
    for s in staff:
        # Check active status
        active = s.get("active", True)
        if isinstance(active, str):
            if active != "有効":
                continue
        elif not active:
            continue

        # Check work_style (働き方) - only 希望シフト staff need to submit
        work_style = s.get("work_style", "")
        if work_style != "希望シフト":
            continue

        required.append(s)
    return required


def find_staff_by_line_uid(line_uid):
    """Find a staff entry by LINE UID from staff master cache."""
    staff = read_staff_master_from_cache()
    for s in staff:
        if s.get("line_uid") == line_uid:
            return s
    return None


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
# Shift period helper
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


# ---------------------------------------------------------------------------
# 4-2. Broadcast shift collection message (filtered to 希望シフト staff)
# ---------------------------------------------------------------------------

def broadcast_shift_collect():
    """Send shift preference collection message to 希望シフト staff individually."""
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

    required_staff = get_shift_input_required_staff()
    sent_count = 0

    for s in required_staff:
        line_uid = s.get("line_uid")
        if not line_uid:
            print(f"  [LINE skip] {s.get('name', 'unknown')}: no LINE UID")
            continue
        try:
            line_push(line_uid, text)
            sent_count += 1
            print(f"  [LINE sent] {s.get('name', 'unknown')}")
        except Exception as e:
            print(f"  [LINE error] {s.get('name', 'unknown')}: {e}")
            slack_notify_admin(
                f"LINE push failed for {s.get('name', 'unknown')} "
                f"(staff_id={s.get('staff_id', '?')}): {e}"
            )

    print(f"LINE broadcast sent to {sent_count}/{len(required_staff)} staff: "
          f"{period_start} ~ {period_end}")


# ---------------------------------------------------------------------------
# 4-3. Handle incoming messages (new kintone 211 structure)
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

    Returns (days_off_list, remarks) or (None, None) if not a shift message.
    days_off_list is a list of normalized date strings.
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
            days_off = [normalize_date(d) for d in parts if d]
            continue

        # Match: 備考
        m = re.match(r"備考[：:\s]*(.+)", line)
        if m:
            remarks = m.group(1).strip()
            continue

    return days_off, remarks


def handle_message(event):
    """Process a LINE text message event.

    Looks up staff by LINE UID, finds their staff_id, and updates
    kintone 211 records' input_status to '入力済' for the matching period.
    New kintone 211 structure: 1 record per person per day.
    """
    reply_token = event["replyToken"]
    user_id = event["source"]["userId"]
    text = event["message"]["text"]

    # Only process messages that look like shift preferences
    days_off, remarks = parse_line_message(text)
    if days_off is None:
        # Not a shift preference message - ignore silently
        return

    # Look up staff by LINE UID
    staff_entry = find_staff_by_line_uid(user_id)
    if not staff_entry:
        user_name = get_line_user_name(user_id)
        line_reply(reply_token,
                   f"{user_name}さんのLINE IDがスタッフマスタに登録されていません。"
                   f"管理者に連絡してください。")
        slack_notify_admin(
            f"Unknown LINE user tried to submit shift: "
            f"uid={user_id}, name={user_name}")
        return

    staff_id = staff_entry.get("staff_id", "")
    staff_name = staff_entry.get("name", "")
    period_start, period_end = get_next_shift_period()

    # Query kintone 211 for this staff's records in the current period
    try:
        query = (
            f'staff_id = "{staff_id}" '
            f'and shift_date >= "{period_start}" '
            f'and shift_date <= "{period_end}"'
        )
        result = kintone_api("records.json", "POST", {
            "app": KINTONE_SHIFT_WISH_APP_ID,
            "query": query,
        })
        records = result.get("records", [])
    except Exception as e:
        print(f"[LINE Error] kintone query failed: {e}")
        line_reply(reply_token, "データの取得中にエラーが発生しました。管理者に連絡してください。")
        slack_notify_admin(f"kintone query error for {staff_name}: {e}")
        return

    # Update records: mark days_off dates as 休み, update input_status
    updates = []
    days_off_set = set(days_off)
    updated_count = 0

    for rec in records:
        rec_id = rec["$id"]["value"]
        shift_date = rec.get("shift_date", {}).get("value", "")

        update_fields = {
            "input_status": {"value": "入力済"},
            "input_channel": {"value": "LINE"},
        }

        # If this date is in the days_off list, mark as 休み
        if shift_date in days_off_set:
            update_fields["shift_type"] = {"value": "休み"}

        # Add remarks if provided and this is the first record
        if remarks and updated_count == 0:
            update_fields["remarks"] = {"value": remarks}

        updates.append({
            "id": rec_id,
            "record": update_fields,
        })
        updated_count += 1

    if not updates:
        line_reply(reply_token,
                   f"対象期間（{period_start} ~ {period_end}）の"
                   f"シフトレコードが見つかりません。管理者に連絡してください。")
        return

    # Batch update kintone records (100 records per batch)
    try:
        for i in range(0, len(updates), 100):
            batch = updates[i:i + 100]
            kintone_api("records.json", "PUT", {
                "app": KINTONE_SHIFT_WISH_APP_ID,
                "records": batch,
            })

        days_off_str = ", ".join(days_off)
        line_reply(reply_token,
                   f"希望を受け付けました\n"
                   f"希望休: {days_off_str}\n"
                   f"更新件数: {updated_count}件")
        print(f"[LINE] Updated: {staff_name} ({staff_id}) "
              f"off={days_off_str}, records={updated_count}")
    except Exception as e:
        error_msg = (f"LINE user={staff_name} ({staff_id}), "
                     f"text={text}, error={e}")
        print(f"[LINE Error] {error_msg}")
        line_reply(reply_token,
                   "登録中にエラーが発生しました。管理者に連絡してください。")
        slack_notify_admin(error_msg)


# ---------------------------------------------------------------------------
# 4-4. Approval notification
# ---------------------------------------------------------------------------

def build_line_approval_message(staff_name, period_start, period_end, work_days):
    """Build LINE message for shift approval notification."""
    work_days_str = "、".join(work_days) if work_days else "なし"
    return (
        f"【シフト確定】\n"
        f"{staff_name}さんのシフトが確定しました。\n\n"
        f"対象期間: {period_start} 〜 {period_end}\n"
        f"出勤日: {work_days_str}\n\n"
        f"詳細はkintoneで確認できます:\n"
        f"https://ny76p.cybozu.com/k/212/"
    )


def send_line_approval_message(staff_name, period_start, period_end, work_days):
    """Send shift approval notification to a staff member via LINE.

    Looks up the staff's LINE UID from staff master cache and sends
    the approval message.
    """
    if not LINE_CHANNEL_ACCESS_TOKEN:
        print(f"[LINE approval skip] No token. staff={staff_name}")
        return

    staff = read_staff_master_from_cache()
    target = None
    for s in staff:
        if s.get("name") == staff_name or s.get("staff_id") == staff_name:
            target = s
            break

    if not target:
        print(f"[LINE approval skip] Staff not found: {staff_name}")
        return

    line_uid = target.get("line_uid")
    if not line_uid:
        print(f"[LINE approval skip] No LINE UID for {staff_name}")
        return

    display_name = target.get("name", staff_name)
    message = build_line_approval_message(
        display_name, period_start, period_end, work_days
    )

    try:
        line_push(line_uid, message)
        print(f"[LINE approval] Sent to {display_name}")
    except Exception as e:
        print(f"[LINE approval error] {display_name}: {e}")
        slack_notify_admin(f"LINE approval notification failed for {display_name}: {e}")


# ---------------------------------------------------------------------------
# 4-5. Reminder for unsubmitted staff
# ---------------------------------------------------------------------------

def send_line_remind(period_start, period_end):
    """Send LINE reminders to 希望シフト staff who haven't submitted.

    Checks kintone 211 for records with input_status != '入力済' for the
    given period, then sends reminders to those staff via LINE.
    """
    if not LINE_CHANNEL_ACCESS_TOKEN:
        print("[LINE remind skip] No LINE_CHANNEL_ACCESS_TOKEN configured.")
        return

    required_staff = get_shift_input_required_staff()
    if not required_staff:
        print("[LINE remind] No 希望シフト staff found.")
        return

    # Check kintone 211 for each staff's submission status
    unsubmitted = []
    for s in required_staff:
        staff_id = s.get("staff_id", "")
        if not staff_id:
            continue

        try:
            query = (
                f'staff_id = "{staff_id}" '
                f'and shift_date >= "{period_start}" '
                f'and shift_date <= "{period_end}" '
                f'and input_status != "入力済"'
            )
            result = kintone_api("records.json", "POST", {
                "app": KINTONE_SHIFT_WISH_APP_ID,
                "query": query,
            })
            records = result.get("records", [])
            if records:
                # Has unsubmitted records
                unsubmitted.append(s)
        except Exception as e:
            print(f"  [kintone error] {s.get('name', '?')}: {e}")

    if not unsubmitted:
        print("[LINE remind] All 希望シフト staff have submitted!")
        return

    deadline = (datetime.now() + timedelta(days=2)).strftime("%Y-%m-%d")
    sent_count = 0

    for s in unsubmitted:
        line_uid = s.get("line_uid")
        if not line_uid:
            print(f"  [LINE remind skip] {s.get('name', '?')}: no LINE UID")
            continue

        name = s.get("name", "スタッフ")
        msg = (
            f"{name}さん、シフト希望がまだ提出されていません。\n\n"
            f"対象期間: {period_start} 〜 {period_end}\n"
            f"回答期限: {deadline}\n\n"
            f"このLINEに以下の形式で返信してください:\n"
            f"希望休 4/5, 4/12"
        )

        try:
            line_push(line_uid, msg)
            sent_count += 1
            print(f"  [LINE remind] {name}")
        except Exception as e:
            print(f"  [LINE remind error] {name}: {e}")
            slack_notify_admin(f"LINE reminder failed for {name}: {e}")

    print(f"LINE reminders sent to {sent_count}/{len(unsubmitted)} staff.")


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
        print("Commands: serve, broadcast, remind, approval")
        sys.exit(1)

    cmd = sys.argv[1]
    if cmd == "serve":
        port = int(sys.argv[2]) if len(sys.argv) > 2 else 8443
        serve(port)
    elif cmd == "broadcast":
        broadcast_shift_collect()
    elif cmd == "remind":
        period_start, period_end = get_next_shift_period()
        if len(sys.argv) >= 4:
            period_start = sys.argv[2]
            period_end = sys.argv[3]
        send_line_remind(period_start, period_end)
    elif cmd == "approval":
        # Usage: python3 line_bot.py approval <staff_name_or_id> <period_start> <period_end> [work_days...]
        if len(sys.argv) < 5:
            print("Usage: python3 line_bot.py approval <staff> <period_start> <period_end> [day1 day2 ...]")
            sys.exit(1)
        staff = sys.argv[2]
        p_start = sys.argv[3]
        p_end = sys.argv[4]
        work_days = sys.argv[5:] if len(sys.argv) > 5 else []
        send_line_approval_message(staff, p_start, p_end, work_days)
    else:
        print(f"Unknown command: {cmd}")
        sys.exit(1)


if __name__ == "__main__":
    main()
