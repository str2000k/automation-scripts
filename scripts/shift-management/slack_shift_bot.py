#!/usr/bin/env python3
"""STEP 3: Slack Bot for shift preference collection (Block Kit edition).

Features:
3-1. Post shift preference collection message to channel (Block Kit)
3-2. Parse staff thread replies and save to kintone
3-3. Send reminder to channel for staff who haven't responded
3-4. Check if all 希望シフト staff submitted and notify admin

Usage:
    python3 slack_shift_bot.py collect          # Post collection message
    python3 slack_shift_bot.py parse            # Parse thread replies -> kintone
    python3 slack_shift_bot.py remind           # Send reminders for non-responders
    python3 slack_shift_bot.py check_submitted  # Check all submitted & notify
"""
import sys
import json
import re
import base64
import urllib.request
import urllib.error
import urllib.parse
from datetime import datetime, timedelta
from config import (
    KINTONE_DOMAIN, KINTONE_USERNAME, KINTONE_PASSWORD,
    KINTONE_SHIFT_WISH_APP_ID, SHIFT_SPREADSHEET_ID,
    SLACK_BOT_TOKEN, SLACK_SHIFT_CHANNEL, SLACK_ADMIN_ID,
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
# Google Sheets helpers (read staff master via cache)
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
# Staff filtering helpers
# ---------------------------------------------------------------------------

def _get_staff_field(staff, en_key, jp_key, default=None):
    """Get a staff field supporting both English (cache) and Japanese (sheet) keys."""
    if en_key in staff:
        return staff[en_key]
    if jp_key in staff:
        return staff[jp_key]
    return default


def _is_active(staff):
    """Check if a staff member is active. Handles both dict formats."""
    val = _get_staff_field(staff, "active", "有効フラグ", True)
    if isinstance(val, bool):
        return val
    if isinstance(val, str):
        return val in ("有効", "true", "True", "1", "yes")
    return bool(val)


def _get_shift_type(staff):
    """Get shift_type / 働き方 field."""
    return _get_staff_field(staff, "shift_type", "働き方", "")


def _get_name(staff):
    """Get staff name from either key format."""
    return _get_staff_field(staff, "name", "氏名", "")


def _get_slack_id(staff):
    """Get Slack ID from either key format."""
    return _get_staff_field(staff, "slack_id", "Slack ID", "")


def _get_line_uid(staff):
    """Get LINE UID from either key format."""
    return _get_staff_field(staff, "line_uid", "LINE UID", "")


def _get_staff_id(staff):
    """Get staff_id from either key format."""
    return _get_staff_field(staff, "staff_id", "staff_id", "")


def get_shift_input_required_staff(all_staff):
    """Filter to staff with shift_type == '希望シフト' and active == True.

    These are the staff who need to submit shift preferences.
    """
    return [
        s for s in all_staff
        if _is_active(s) and _get_shift_type(s) == "希望シフト"
    ]


def get_all_active_staff(all_staff):
    """Filter to all active staff regardless of shift_type."""
    return [s for s in all_staff if _is_active(s)]


# ---------------------------------------------------------------------------
# kintone query helpers
# ---------------------------------------------------------------------------

def fetch_submitted_staff_ids(period_start, period_end):
    """Query kintone app 211 for staff who have input_status='入力済' in the period.

    Returns set of staff_ids.
    """
    submitted = set()
    try:
        query = (
            f'shift_date >= "{period_start}" '
            f'and shift_date <= "{period_end}" '
            f'and input_status in ("入力済")'
        )
        body = {
            "app": KINTONE_SHIFT_WISH_APP_ID,
            "query": query,
            "fields": ["staff_id"],
        }
        records = kintone_api("records.json", method="GET", body=body)
        for r in records.get("records", []):
            sid = r.get("staff_id", {}).get("value", "")
            if sid:
                submitted.add(sid)
    except Exception as e:
        print(f"Warning: Could not query kintone for submitted staff: {e}")
    return submitted


def get_missing_staff(period_start, period_end, all_staff):
    """Return list of 希望シフト staff who haven't submitted for the period."""
    required = get_shift_input_required_staff(all_staff)
    submitted_ids = fetch_submitted_staff_ids(period_start, period_end)
    return [s for s in required if _get_staff_id(s) not in submitted_ids]


def check_all_submitted(period_start, period_end, all_staff):
    """Check if all 希望シフト staff have submitted.

    Returns (all_done: bool, missing: list).
    """
    missing = get_missing_staff(period_start, period_end, all_staff)
    return (len(missing) == 0, missing)


# ---------------------------------------------------------------------------
# Button-based shift input (Slack Bolt integration)
# ---------------------------------------------------------------------------

def build_shift_input_blocks(staff_id, staff_name, period_start, period_end, deadline):
    """Build Block Kit blocks with per-day shift buttons for DM."""
    weekdays = ["月", "火", "水", "木", "金", "土", "日"]

    blocks = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": "📅 シフト希望の入力をお願いします"},
        },
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*対象期間:*\n{period_start} 〜 {period_end}"},
                {"type": "mrkdwn", "text": f"*回答期限:*\n{deadline}"},
            ],
        },
        {"type": "divider"},
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": "各日付のシフト希望をボタンで選択してください👇",
            },
        },
    ]

    current = datetime.strptime(period_start, "%Y-%m-%d").date()
    end = datetime.strptime(period_end, "%Y-%m-%d").date()
    while current <= end:
        date_str = str(current)
        weekday = weekdays[current.weekday()]

        blocks.append({
            "type": "section",
            "block_id": f"shift_{date_str}",
            "text": {
                "type": "mrkdwn",
                "text": f"*{date_str}（{weekday}）*",
            },
        })
        blocks.append({
            "type": "actions",
            "block_id": f"actions_{date_str}",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "✅ 出勤"},
                    "style": "primary",
                    "value": staff_id,
                    "action_id": f"shift_{date_str}_出勤",
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "😴 休み"},
                    "value": staff_id,
                    "action_id": f"shift_{date_str}_休み",
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "🙏 希望休"},
                    "style": "danger",
                    "value": staff_id,
                    "action_id": f"shift_{date_str}_希望休",
                },
            ],
        })
        current += timedelta(days=1)

    blocks.append({"type": "divider"})
    blocks.append({
        "type": "context",
        "elements": [
            {"type": "mrkdwn", "text": f"kintoneでも入力できます: <https://ny76p.cybozu.com/k/211/|シフト希望収集アプリ>"},
        ],
    })

    return blocks


def send_shift_input_dm(staff, period_start, period_end, deadline):
    """Send button-based shift input DM to a staff member.

    Args:
        staff: dict with keys staff_id, name (or 氏名), slack_id (or Slack ID)
        period_start: YYYY-MM-DD
        period_end: YYYY-MM-DD
        deadline: YYYY-MM-DD
    """
    slack_id = _get_slack_id(staff)
    if not slack_id:
        print(f"  [SKIP] {_get_name(staff)}: slack_id未設定")
        return

    staff_id = _get_staff_id(staff)
    staff_name = _get_name(staff)

    blocks = build_shift_input_blocks(
        staff_id=staff_id,
        staff_name=staff_name,
        period_start=period_start,
        period_end=period_end,
        deadline=deadline,
    )

    result = slack_api("chat.postMessage", json_body={
        "channel": slack_id,
        "blocks": blocks,
        "text": f"【シフト希望入力のお願い】{period_start}〜{period_end} 回答期限: {deadline}",
    })

    if result.get("ok"):
        print(f"  [SENT] {staff_name}（{slack_id}）へDM送信")
    else:
        print(f"  [ERROR] {staff_name}: {result.get('error')}")


# ---------------------------------------------------------------------------
# Block Kit message builders
# ---------------------------------------------------------------------------

def build_collect_message(period_start, period_end, deadline, staff_list):
    """Build Block Kit message for shift collection start.

    staff_list should be the filtered 希望シフト staff only.
    """
    mention_list = " ".join(
        f"<@{_get_slack_id(s)}>" for s in staff_list if _get_slack_id(s)
    )
    staff_names = ", ".join(_get_name(s) for s in staff_list if _get_name(s))

    blocks = [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": "シフト希望収集のお知らせ",
            },
        },
        {
            "type": "section",
            "fields": [
                {
                    "type": "mrkdwn",
                    "text": f"*対象期間:*\n{period_start} 〜 {period_end}",
                },
                {
                    "type": "mrkdwn",
                    "text": f"*回答期限:*\n{deadline}",
                },
            ],
        },
        {"type": "divider"},
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    "このスレッドに以下の形式で返信してください:\n"
                    "```\n"
                    "希望休: 2026-03-16, 2026-03-20\n"
                    "備考: 午前のみ希望（3/18）\n"
                    "```"
                ),
            },
        },
        {"type": "divider"},
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": f"*対象スタッフ ({len(staff_list)}名):* {staff_names}",
                },
            ],
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": mention_list or "(対象スタッフなし)",
            },
        },
    ]

    fallback_text = (
        f"シフト希望収集: {period_start} 〜 {period_end} (期限: {deadline})"
    )
    return blocks, fallback_text


def build_remind_message(missing_staff):
    """Build Block Kit reminder message for missing staff."""
    if not missing_staff:
        return [], "全員提出済みです"

    mention_list = " ".join(
        f"<@{_get_slack_id(s)}>" for s in missing_staff if _get_slack_id(s)
    )
    names = ", ".join(_get_name(s) for s in missing_staff if _get_name(s))

    blocks = [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": "シフト希望 未提出リマインド",
            },
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"以下の *{len(missing_staff)}名* がまだシフト希望を提出していません。\n"
                    f"お早めにスレッドへ返信をお願いします。"
                ),
            },
        },
        {"type": "divider"},
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*未提出:* {names}",
            },
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": mention_list or "(Slack ID未設定)",
            },
        },
    ]

    fallback_text = f"シフト希望未提出リマインド: {names}"
    return blocks, fallback_text


def build_all_submitted_message(period_start, period_end, admin_slack_id):
    """Build Block Kit notification when all 希望シフト staff have submitted."""
    blocks = [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": "全員提出完了",
            },
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"*{period_start} 〜 {period_end}* のシフト希望が全員揃いました。\n"
                    f"シフト生成を開始できます。"
                ),
            },
        },
        {"type": "divider"},
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"<@{admin_slack_id}> 確認をお願いします。",
            },
        },
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {
                        "type": "plain_text",
                        "text": "シフト生成を開始",
                    },
                    "style": "primary",
                    "action_id": "start_shift_generation",
                    "value": f"{period_start}_{period_end}",
                },
            ],
        },
    ]

    fallback_text = (
        f"シフト希望 全員提出完了 ({period_start} 〜 {period_end})"
    )
    return blocks, fallback_text


def build_approval_message(period_start, period_end, schedule_version, all_active_staff):
    """Build Block Kit approval notification mentioning ALL active staff."""
    mention_list = " ".join(
        f"<@{_get_slack_id(s)}>" for s in all_active_staff if _get_slack_id(s)
    )
    staff_count = len(all_active_staff)

    blocks = [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": "シフト確定のお知らせ",
            },
        },
        {
            "type": "section",
            "fields": [
                {
                    "type": "mrkdwn",
                    "text": f"*対象期間:*\n{period_start} 〜 {period_end}",
                },
                {
                    "type": "mrkdwn",
                    "text": f"*バージョン:*\n{schedule_version}",
                },
            ],
        },
        {"type": "divider"},
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"シフトが承認・確定されました。\n"
                    f"対象: *{staff_count}名* の全アクティブスタッフ\n"
                    f"各自のシフトを確認してください。"
                ),
            },
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": mention_list or "(対象スタッフなし)",
            },
        },
    ]

    fallback_text = (
        f"シフト確定通知: {period_start} 〜 {period_end} ({schedule_version})"
    )
    return blocks, fallback_text


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
    required_staff = get_shift_input_required_staff(staff)

    if not required_staff:
        print("No staff require shift input (希望シフト). Skipping.")
        return

    blocks, fallback_text = build_collect_message(
        period_start, period_end, deadline, required_staff
    )

    result = slack_api("chat.postMessage", json_body={
        "channel": SLACK_SHIFT_CHANNEL,
        "text": fallback_text,
        "blocks": blocks,
    })

    if result.get("ok"):
        ts = result["ts"]
        channel = result["channel"]
        print(f"Collection message posted: channel={channel}, ts={ts}")
        # Save thread ts for later parsing
        state = {
            "channel": channel,
            "ts": ts,
            "period_start": period_start,
            "period_end": period_end,
        }
        state_path = _state_file()
        with open(state_path, "w") as f:
            json.dump(state, f, indent=2)
        print(f"State saved to {state_path}")

        # Send button-based DMs to each staff
        print(f"Sending button DMs to {len(required_staff)} staff...")
        for s in required_staff:
            send_shift_input_dm(s, period_start, period_end, deadline)

        # Send LINE notifications to required staff
        for s in required_staff:
            uid = _get_line_uid(s)
            if uid:
                line_push(uid,
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
        if _get_slack_id(s) == user_id:
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
    submitted_ids = fetch_submitted_staff_ids(state["period_start"], state["period_end"])

    registered = 0
    for msg in messages:
        user_id = msg.get("user", "")
        staff_entry = slack_user_to_staff(user_id, staff)
        if not staff_entry:
            print(f"  Unknown user: {user_id}, skipping")
            continue
        staff_id = _get_staff_id(staff_entry)
        staff_name = _get_name(staff_entry)
        if staff_id in submitted_ids:
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
            "shift_date": {"value": state["period_start"]},
            "desired_days_off": {"value": days_off},
            "remarks": {"value": remarks},
            "input_channel": {"value": "Slack"},
            "submitted_at": {"value": now},
            "input_status": {"value": "入力済"},
        }
        try:
            kintone_api("record.json", "POST", {
                "app": KINTONE_SHIFT_WISH_APP_ID,
                "record": record,
            })
            submitted_ids.add(staff_id)
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

    # After parsing, check if all submitted
    all_done, missing = check_all_submitted(
        state["period_start"], state["period_end"], staff
    )
    if all_done:
        print("All 希望シフト staff have submitted! Posting notification.")
        _post_all_submitted_notification(state["period_start"], state["period_end"])


# ---------------------------------------------------------------------------
# 3-3. Reminder
# ---------------------------------------------------------------------------

def send_reminders():
    """Send reminder to channel for staff who haven't responded."""
    state = load_state()
    staff = read_staff_master_from_sheets()

    missing = get_missing_staff(state["period_start"], state["period_end"], staff)

    if not missing:
        print("All 希望シフト staff have responded!")
        return

    blocks, fallback_text = build_remind_message(missing)

    print(f"Posting reminder for {len(missing)} staff to channel...")
    result = slack_api("chat.postMessage", json_body={
        "channel": SLACK_SHIFT_CHANNEL,
        "thread_ts": state["ts"],
        "text": fallback_text,
        "blocks": blocks,
    })

    if result.get("ok"):
        print(f"Reminder posted to channel thread.")
    else:
        print(f"Error posting reminder: {result.get('error')}")

    # Also send LINE notifications to missing staff
    for s in missing:
        uid = _get_line_uid(s)
        name = _get_name(s)
        if uid:
            line_push(uid,
                f"{name}さん、シフト希望がまだ提出されていません。\n"
                f"対象期間: {state['period_start']} 〜 {state['period_end']}\n"
                f"Slackの #{SLACK_SHIFT_CHANNEL} のスレッドで返信してください。")
            print(f"  [LINE] {name}")


# ---------------------------------------------------------------------------
# 3-4. Check all submitted
# ---------------------------------------------------------------------------

def _post_all_submitted_notification(period_start, period_end):
    """Post all-submitted notification to channel."""
    blocks, fallback_text = build_all_submitted_message(
        period_start, period_end, SLACK_ADMIN_ID
    )
    result = slack_api("chat.postMessage", json_body={
        "channel": SLACK_SHIFT_CHANNEL,
        "text": fallback_text,
        "blocks": blocks,
    })
    if result.get("ok"):
        print("All-submitted notification posted.")
    else:
        print(f"Error posting all-submitted notification: {result.get('error')}")


def cmd_check_submitted():
    """Command handler: check if all 希望シフト staff submitted."""
    state = load_state()
    staff = read_staff_master_from_sheets()

    all_done, missing = check_all_submitted(
        state["period_start"], state["period_end"], staff
    )

    if all_done:
        print("All 希望シフト staff have submitted!")
        _post_all_submitted_notification(state["period_start"], state["period_end"])
    else:
        names = ", ".join(_get_name(s) for s in missing)
        print(f"Still waiting on {len(missing)} staff: {names}")


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
        print("Commands: collect, parse, remind, check_submitted, sync")
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
    elif cmd == "check_submitted":
        cmd_check_submitted()
    elif cmd == "sync":
        sync_staff_master()
    else:
        print(f"Unknown command: {cmd}")
        sys.exit(1)


if __name__ == "__main__":
    main()
