#!/usr/bin/env python3
"""STEP 3: Slack Bot for shift preference collection.

Features:
3-1. Post shift preference collection message to channel
3-2. Parse staff thread replies and save to kintone
3-3. Send reminder DMs to staff who haven't responded

Usage:
    python3 slack_shift_bot.py collect   # Post collection message
    python3 slack_shift_bot.py parse     # Parse thread replies -> kintone
    python3 slack_shift_bot.py remind    # Send reminders to non-responders
"""
import sys
import json
import re
import base64
import urllib.request
import urllib.error
from datetime import datetime, timedelta
from config import (
    KINTONE_DOMAIN, KINTONE_USERNAME, KINTONE_PASSWORD,
    KINTONE_SHIFT_WISH_APP_ID, SHIFT_SPREADSHEET_ID,
    SLACK_BOT_TOKEN, SLACK_SHIFT_CHANNEL,
    LINE_CHANNEL_ACCESS_TOKEN, LINE_CHANNEL_SECRET,
)


# ---------------------------------------------------------------------------
# API helpers
# ---------------------------------------------------------------------------

def slack_api(method, params=None, json_body=None):
    url = f"https://slack.com/api/{method}"
    headers = {"Authorization": f"Bearer {SLACK_BOT_TOKEN}"}
    if json_body:
        headers["Content-Type"] = "application/json"
        data = json.dumps(json_body).encode()
        req = urllib.request.Request(url, data=data, headers=headers)
    elif params:
        qs = urllib.parse.urlencode(params)
        req = urllib.request.Request(f"{url}?{qs}", headers=headers)
    else:
        req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read())


def kintone_api(path, method="GET", body=None):
    url = f"https://{KINTONE_DOMAIN}/k/v1/{path}"
    credential = base64.b64encode(f"{KINTONE_USERNAME}:{KINTONE_PASSWORD}".encode()).decode()
    headers = {"X-Cybozu-Authorization": credential}
    if body:
        headers["Content-Type"] = "application/json"
        data = json.dumps(body).encode()
    else:
        data = None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read())


def line_push(user_id, message):
    if not LINE_CHANNEL_ACCESS_TOKEN:
        print(f"  [LINE skip] No token. Would send to {user_id}: {message}")
        return
    url = "https://api.line.me/v2/bot/message/push"
    body = {
        "to": user_id,
        "messages": [{"type": "text", "text": message}],
    }
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}",
    }
    data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, headers=headers)
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read())


# ---------------------------------------------------------------------------
# Google Sheets helpers (read staff master via Sheets API v4)
# ---------------------------------------------------------------------------

def read_staff_master_from_sheets():
    """Read staff master from Google Spreadsheet via gw-chillaxy MCP.

    Falls back to a hardcoded sample if spreadsheet is not accessible.
    Returns list of dicts with keys: name, employment_type, max_hours,
    forced_days, skills, slack_id, line_uid, active
    """
    # For now, read from a local cache file if available
    import os
    cache_path = os.path.join(os.path.dirname(__file__), "staff_master_cache.json")
    if os.path.exists(cache_path):
        with open(cache_path) as f:
            return json.load(f)

    print("WARNING: staff_master_cache.json not found.")
    print("Run 'python3 sync_staff_master.py' to fetch from Google Sheets.")
    return []


# ---------------------------------------------------------------------------
# 3-1. Post collection message
# ---------------------------------------------------------------------------

def get_next_shift_period():
    """Calculate next 2-week shift period (Mon-Sun x2)."""
    today = datetime.now()
    # Next Monday
    days_ahead = 7 - today.weekday()  # 0=Mon
    if days_ahead <= 0:
        days_ahead += 7
    start = today + timedelta(days=days_ahead)
    end = start + timedelta(days=13)
    return start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")


def post_collection_message():
    """Post shift preference collection message to Slack channel."""
    period_start, period_end = get_next_shift_period()
    deadline = (datetime.now() + timedelta(days=5)).strftime("%Y-%m-%d")

    staff = read_staff_master_from_sheets()
    active_staff = [s for s in staff if s.get("active", True)]
    mention_list = " ".join(
        f"<@{s['slack_id']}>" for s in active_staff if s.get("slack_id")
    )

    text = (
        f"*シフト希望収集のお知らせ*\n\n"
        f"対象期間: *{period_start} 〜 {period_end}*\n"
        f"回答期限: *{deadline}*\n\n"
        f"このスレッドに以下の形式で返信してください:\n"
        f"```\n"
        f"希望休: 2026-03-16, 2026-03-20\n"
        f"備考: 午前のみ希望（3/18）\n"
        f"```\n\n"
        f"対象スタッフ: {mention_list}\n"
    )

    result = slack_api("chat.postMessage", json_body={
        "channel": SLACK_SHIFT_CHANNEL,
        "text": text,
        "mrkdwn": True,
    })

    if result.get("ok"):
        ts = result["ts"]
        channel = result["channel"]
        print(f"Collection message posted: channel={channel}, ts={ts}")
        # Save thread ts for later parsing
        state = {"channel": channel, "ts": ts, "period_start": period_start, "period_end": period_end}
        state_path = _state_file()
        with open(state_path, "w") as f:
            json.dump(state, f, indent=2)
        print(f"State saved to {state_path}")

        # Send LINE notifications
        for s in active_staff:
            if s.get("line_uid"):
                line_push(s["line_uid"],
                    f"シフト希望の提出をお願いします。\n"
                    f"期間: {period_start} 〜 {period_end}\n"
                    f"期限: {deadline}\n"
                    f"Slackの #{SLACK_SHIFT_CHANNEL} スレッドで返信してください。")
    else:
        print(f"Error posting message: {result.get('error')}")


# ---------------------------------------------------------------------------
# 3-2. Parse thread replies -> kintone
# ---------------------------------------------------------------------------

def _state_file():
    import os
    return os.path.join(os.path.dirname(__file__), "collection_state.json")


def load_state():
    import os
    path = _state_file()
    if not os.path.exists(path):
        print("Error: No active collection. Run 'collect' first.")
        sys.exit(1)
    with open(path) as f:
        return json.load(f)


def parse_shift_reply(text):
    """Parse a staff reply into desired_days_off and remarks."""
    days_off = ""
    remarks = ""

    for line in text.strip().split("\n"):
        line = line.strip()
        m_off = re.match(r"(?:希望休|休み|休日)[：:\s]*(.+)", line)
        m_rem = re.match(r"(?:備考|メモ|その他)[：:\s]*(.+)", line)
        if m_off:
            days_off = m_off.group(1).strip()
        elif m_rem:
            remarks = m_rem.group(1).strip()
        elif not days_off and re.match(r"[\d\-/,\s]+$", line):
            # Plain date list
            days_off = line.strip()

    return days_off, remarks


def slack_user_to_staff(user_id, staff_list):
    """Map Slack user ID to staff entry (with staff_id and name)."""
    for s in staff_list:
        if s.get("slack_id") == user_id:
            return s
    return None


def parse_thread_replies():
    """Parse Slack thread replies and register to kintone."""
    state = load_state()
    staff = read_staff_master_from_sheets()

    # Get thread replies
    result = slack_api("conversations.replies", params={
        "channel": state["channel"],
        "ts": state["ts"],
    })

    if not result.get("ok"):
        print(f"Error reading thread: {result.get('error')}")
        return

    messages = result.get("messages", [])[1:]  # Skip parent message
    if not messages:
        print("No replies yet.")
        return

    # Check already-registered staff in kintone (by staff_id)
    existing = set()
    try:
        query = (
            f'target_period_start = "{state["period_start"]}" '
            f'and target_period_end = "{state["period_end"]}"'
        )
        records = kintone_api(
            f"records.json?app={KINTONE_SHIFT_WISH_APP_ID}&query={urllib.parse.quote(query)}"
        )
        for r in records.get("records", []):
            sid = r.get("staff_id", {}).get("value", "")
            if sid:
                existing.add(sid)
            else:
                existing.add(r["staff_name"]["value"])  # fallback
    except Exception as e:
        print(f"Warning: Could not check existing records: {e}")

    registered = 0
    for msg in messages:
        user_id = msg.get("user", "")
        staff_entry = slack_user_to_staff(user_id, staff)
        if not staff_entry:
            print(f"  Unknown user: {user_id}, skipping")
            continue
        staff_id = staff_entry.get("staff_id", "")
        staff_name = staff_entry["name"]
        lookup_key = staff_id or staff_name
        if lookup_key in existing:
            print(f"  {staff_name} ({staff_id}): already registered, skipping")
            continue

        days_off, remarks = parse_shift_reply(msg.get("text", ""))
        if not days_off and not remarks:
            print(f"  {staff_name}: could not parse reply, skipping")
            continue

        # Register to kintone
        now = datetime.now().strftime("%Y-%m-%dT%H:%M:%S+09:00")
        record = {
            "staff_id": {"value": staff_id},
            "staff_name": {"value": staff_name},
            "target_period_start": {"value": state["period_start"]},
            "target_period_end": {"value": state["period_end"]},
            "desired_days_off": {"value": days_off},
            "remarks": {"value": remarks},
            "input_channel": {"value": "Slack"},
            "submitted_at": {"value": now},
            "status": {"value": "未処理"},
        }
        try:
            kintone_api("record.json", "POST", {
                "app": KINTONE_SHIFT_WISH_APP_ID,
                "record": record,
            })
            existing.add(lookup_key)
            registered += 1
            print(f"  {staff_name} ({staff_id}): registered (off={days_off})")

            # Reply confirmation in thread
            slack_api("chat.postMessage", json_body={
                "channel": state["channel"],
                "thread_ts": state["ts"],
                "text": f"<@{user_id}> 希望を受け付けました",
            })
        except Exception as e:
            print(f"  {staff_name}: kintone error: {e}")

    print(f"\nRegistered {registered} new entries.")


# ---------------------------------------------------------------------------
# 3-3. Reminder
# ---------------------------------------------------------------------------

def send_reminders():
    """Send DM reminders to staff who haven't responded."""
    state = load_state()
    staff = read_staff_master_from_sheets()
    active_staff = [s for s in staff if s.get("active", True)]

    # Get already-registered staff (by staff_id)
    registered_ids = set()
    try:
        query = (
            f'target_period_start = "{state["period_start"]}" '
            f'and target_period_end = "{state["period_end"]}"'
        )
        records = kintone_api(
            f"records.json?app={KINTONE_SHIFT_WISH_APP_ID}&query={urllib.parse.quote(query)}"
        )
        for r in records.get("records", []):
            sid = r.get("staff_id", {}).get("value", "")
            if sid:
                registered_ids.add(sid)
            else:
                registered_ids.add(r["staff_name"]["value"])
    except Exception as e:
        print(f"Warning: Could not check kintone: {e}")

    missing = [s for s in active_staff
               if (s.get("staff_id") or s["name"]) not in registered_ids]

    if not missing:
        print("All staff have responded!")
        return

    print(f"Sending reminders to {len(missing)} staff...")
    for s in missing:
        name = s["name"]
        slack_id = s.get("slack_id")
        line_uid = s.get("line_uid")

        msg = (
            f"{name}さん、シフト希望がまだ提出されていません。\n"
            f"対象期間: {state['period_start']} 〜 {state['period_end']}\n"
            f"#{SLACK_SHIFT_CHANNEL} のスレッドで返信してください。"
        )

        if slack_id:
            # Open DM and send
            dm = slack_api("conversations.open", json_body={"users": slack_id})
            if dm.get("ok"):
                slack_api("chat.postMessage", json_body={
                    "channel": dm["channel"]["id"],
                    "text": msg,
                })
                print(f"  [Slack DM] {name}")

        if line_uid:
            line_push(line_uid, msg)
            print(f"  [LINE] {name}")


# ---------------------------------------------------------------------------
# Sync staff master from Google Sheets (helper)
# ---------------------------------------------------------------------------

def sync_staff_master():
    """Placeholder: sync staff master from Google Sheets to local cache.
    In production, this would use the Google Sheets API directly.
    For now, use the MCP tools or run this manually."""
    print("To sync staff master, use Claude Code MCP:")
    print("  mcp__gw-chillaxy__read_sheet_values")
    print("Then save the result to staff_master_cache.json")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    if len(sys.argv) < 2:
        print("Usage: python3 slack_shift_bot.py <command>")
        print("Commands: collect, parse, remind, sync")
        sys.exit(1)

    cmd = sys.argv[1]
    if cmd == "collect":
        post_collection_message()
        # Also broadcast via LINE
        try:
            from line_bot import broadcast_shift_collect
            broadcast_shift_collect()
        except Exception as e:
            print(f"LINE broadcast skipped: {e}")
    elif cmd == "parse":
        parse_thread_replies()
    elif cmd == "remind":
        send_reminders()
    elif cmd == "sync":
        sync_staff_master()
    else:
        print(f"Unknown command: {cmd}")
        sys.exit(1)


if __name__ == "__main__":
    import urllib.parse
    main()
