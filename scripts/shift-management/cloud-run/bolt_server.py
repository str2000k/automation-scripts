#!/usr/bin/env python3
"""
bolt_server.py

Slack Bolt server for shift button interactions (Socket Mode).
Receives button taps from staff and updates kintone ID=211.

Two-step flow for 出勤:
  Step 1: [出勤] [休み] [希望休]
  Step 2: [フル 10-19] [早番 10-15] [遅番 14-19] [戻る]

No public URL required - uses WebSocket via Socket Mode.

Usage:
  python3 bolt_server.py
"""

import base64
import datetime
import json
import logging
import os
import re
import sys
import urllib.parse
import urllib.request

from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

from config import (
    SLACK_BOT_TOKEN,
    SLACK_APP_TOKEN,
    BOLT_PORT,
    KINTONE_DOMAIN,
    KINTONE_USERNAME,
    KINTONE_PASSWORD,
    KINTONE_SHIFT_WISH_APP_ID,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Bolt app (Socket Mode)
bolt_app = App(token=SLACK_BOT_TOKEN)

SHIFT_CHANNEL = "C0AKBJ1LTV2"  # #shift-management

# -----------------------------------------------------------------------
# kintone helpers
# -----------------------------------------------------------------------

def _kintone_headers():
    credential = base64.b64encode(
        f"{KINTONE_USERNAME}:{KINTONE_PASSWORD}".encode()
    ).decode()
    return {
        "X-Cybozu-Authorization": credential,
        "Content-Type": "application/json",
    }


def upsert_kintone_shift(staff_id: str, shift_date: str, shift_status: str,
                          start_time: str = "", end_time: str = "") -> bool:
    """Upsert kintone ID=211 record (staff_id x shift_date).

    Creates record if not found, updates if exists.
    """
    headers = _kintone_headers()

    # Search for existing record
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

    now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    record_fields = {
        "shift_type": {"value": shift_status},
        "input_status": {"value": "入力済"},
        "input_channel": {"value": "Slack"},
        "submitted_at": {"value": now},
    }
    if start_time:
        record_fields["start_time"] = {"value": start_time}
        record_fields["end_time"] = {"value": end_time or start_time}
        record_fields["work_time_type"] = {"value": "時間指定"}
    else:
        record_fields["work_time_type"] = {"value": "フリー"}

    records = data.get("records", [])

    if records:
        # Update existing
        record_id = records[0]["$id"]["value"]
        payload = {
            "app": KINTONE_SHIFT_WISH_APP_ID,
            "id": record_id,
            "record": record_fields,
        }
        req = urllib.request.Request(
            f"https://{KINTONE_DOMAIN}/k/v1/record.json",
            data=json.dumps(payload).encode(),
            headers=headers,
            method="PUT",
        )
    else:
        # Create new
        record_fields["staff_id"] = {"value": staff_id}
        record_fields["shift_date"] = {"value": shift_date}
        # Compute period: 1-15日 → period=当月1-15, 16-末日 → period=当月16-末日
        d = datetime.date.fromisoformat(shift_date)
        if d.day <= 15:
            p_start = d.replace(day=1).isoformat()
            p_end = d.replace(day=15).isoformat()
        else:
            import calendar
            p_start = d.replace(day=16).isoformat()
            last_day = calendar.monthrange(d.year, d.month)[1]
            p_end = d.replace(day=last_day).isoformat()
        record_fields["target_period_start"] = {"value": p_start}
        record_fields["target_period_end"] = {"value": p_end}
        payload = {
            "app": KINTONE_SHIFT_WISH_APP_ID,
            "record": record_fields,
        }
        req = urllib.request.Request(
            f"https://{KINTONE_DOMAIN}/k/v1/record.json",
            data=json.dumps(payload).encode(),
            headers=headers,
            method="POST",
        )

    try:
        with urllib.request.urlopen(req) as resp:
            json.loads(resp.read())
    except urllib.request.HTTPError as e:
        body_text = e.read().decode() if hasattr(e, 'read') else ""
        logger.error(f"kintone upsert failed: {e} | {body_text}")
    except Exception as e:
        logger.error(f"kintone upsert failed: {e}")
        return False

    action = "updated" if records else "created"
    time_info = f" ({start_time}-{end_time})" if start_time else ""
    logger.info(f"kintone {action}: {staff_id} / {shift_date} -> {shift_status}{time_info}")
    return True


# -----------------------------------------------------------------------
# Block builders
# -----------------------------------------------------------------------

def _weekday_str(date_str: str) -> str:
    weekdays = ["月", "火", "水", "木", "金", "土", "日"]
    d = datetime.date.fromisoformat(date_str)
    return weekdays[d.weekday()]


def build_confirmed_blocks(date_str: str, staff_id: str, shift_status: str,
                            start_time: str = "", end_time: str = "") -> list:
    """Return blocks showing the selected status + 修正 button."""
    weekday = _weekday_str(date_str)
    emoji = {"出勤": "✅", "休み": "😴", "希望休": "🙏"}.get(shift_status, "")
    time_info = f"（{start_time}〜{end_time}）" if start_time else ""
    section = {
        "type": "section",
        "block_id": f"shift_{date_str}",
        "text": {
            "type": "mrkdwn",
            "text": f"*{date_str}（{weekday}）*　→　{emoji} *{shift_status}*{time_info}",
        },
        "accessory": {
            "type": "button",
            "text": {"type": "plain_text", "text": "✏️ 修正"},
            "value": staff_id,
            "action_id": f"edit_{date_str}",
        },
    }
    return [section]


# 30-min interval time options (08:00 - 23:00)
TIME_OPTIONS = []
for _h in range(8, 24):
    for _m in (0, 30):
        t = f"{_h:02d}:{_m:02d}"
        TIME_OPTIONS.append({"text": {"type": "plain_text", "text": t}, "value": t})


def _time_options_block(action_id, label, initial="10:00"):
    """Build a static_select with 30-min interval time slots."""
    initial_option = {"text": {"type": "plain_text", "text": initial}, "value": initial}
    return {
        "type": "static_select",
        "action_id": action_id,
        "placeholder": {"type": "plain_text", "text": label},
        "initial_option": initial_option,
        "options": TIME_OPTIONS,
    }


def build_time_select_blocks(date_str: str, staff_id: str) -> list:
    """Return blocks for time selection (step 2 after 出勤 tap)."""
    weekday = _weekday_str(date_str)
    section = {
        "type": "section",
        "block_id": f"shift_{date_str}",
        "text": {
            "type": "mrkdwn",
            "text": f"*{date_str}（{weekday}）*　→　出勤・退勤時間を選択して確定👇",
        },
    }
    actions = {
        "type": "actions",
        "block_id": f"actions_{date_str}",
        "elements": [
            _time_options_block(f"start_{date_str}", "出勤時間", "10:00"),
            _time_options_block(f"end_{date_str}", "退勤時間", "19:00"),
            {
                "type": "button",
                "text": {"type": "plain_text", "text": "✅ 確定"},
                "style": "primary",
                "value": staff_id,
                "action_id": f"confirm_{date_str}",
            },
            {
                "type": "button",
                "text": {"type": "plain_text", "text": "↩️ 戻る"},
                "value": staff_id,
                "action_id": f"back_{date_str}",
            },
        ],
    }
    return [section, actions]


def build_initial_buttons(date_str: str, staff_id: str) -> list:
    """Return the initial [出勤] [休み] [希望休] blocks for a date."""
    weekday = _weekday_str(date_str)
    section = {
        "type": "section",
        "block_id": f"shift_{date_str}",
        "text": {
            "type": "mrkdwn",
            "text": f"*{date_str}（{weekday}）*",
        },
    }
    actions = {
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
                "text": {"type": "plain_text", "text": "🙏 休み希望"},
                "style": "danger",
                "value": staff_id,
                "action_id": f"shift_{date_str}_希望休",
            },
        ],
    }
    return [section, actions]


def replace_date_blocks(original_blocks, shift_date, new_blocks_for_date):
    """Replace the section(+actions) blocks for a specific date with new blocks.

    Handles both states:
    - section + actions (initial / time-select): replaces both
    - section only with accessory (confirmed): replaces just the section
    """
    result = []
    skip_next_actions = False

    for block in original_blocks:
        block_id = block.get("block_id", "")

        if skip_next_actions:
            if block.get("type") == "actions" and block_id == f"actions_{shift_date}":
                skip_next_actions = False
                continue
            # No actions block follows (confirmed state) — stop skipping
            skip_next_actions = False

        if block_id == f"shift_{shift_date}":
            result.extend(new_blocks_for_date)
            skip_next_actions = True
        else:
            result.append(block)

    return result


def update_message(client, body, new_blocks, fallback_text):
    """Update the original message with new blocks."""
    client.chat_update(
        channel=body["container"]["channel_id"],
        ts=body["container"]["message_ts"],
        blocks=new_blocks,
        text=fallback_text,
    )


# -----------------------------------------------------------------------
# Bolt action handlers
# -----------------------------------------------------------------------

# Step 1: shift_YYYY-MM-DD_出勤 / 休み / 希望休
@bolt_app.action(re.compile(r"^shift_\d{4}-\d{2}-\d{2}_.+$"))
def handle_shift_action(ack, body, client, logger):
    """Handle staff tapping a shift button (step 1)."""
    ack()

    action = body["actions"][0]
    action_id = action["action_id"]
    user_id = body["user"]["id"]
    user_name = body["user"].get("name", user_id)

    parts = action_id.split("_", 2)  # ["shift", "2026-03-09", "出勤"]
    if len(parts) != 3:
        logger.error(f"Invalid action_id: {action_id}")
        return

    shift_date = parts[1]
    shift_status = parts[2]
    staff_id = action.get("value", "")

    logger.info(f"Shift action: {staff_id} / {shift_date} / {shift_status} by {user_name}")

    if shift_status == "出勤":
        # Step 2: Show time selection buttons
        new_blocks = replace_date_blocks(
            body["message"]["blocks"],
            shift_date,
            build_time_select_blocks(shift_date, staff_id),
        )
        update_message(client, body, new_blocks,
                       f"シフト希望入力（{shift_date}: 時間帯選択中）")
        return

    # 休み / 希望休: save immediately
    success = upsert_kintone_shift(staff_id, shift_date, shift_status)

    if not success:
        client.chat_postMessage(
            channel=user_id,
            text=f"❌ kintone の更新に失敗しました。({shift_date} / {shift_status})\n管理者に連絡してください。",
        )
        return

    new_blocks = replace_date_blocks(
        body["message"]["blocks"],
        shift_date,
        build_confirmed_blocks(shift_date, staff_id, shift_status),
    )
    update_message(client, body, new_blocks,
                   f"シフト希望入力（{shift_date}: {shift_status}）")


# Step 2: confirm_YYYY-MM-DD — read timepicker values and save
@bolt_app.action(re.compile(r"^confirm_\d{4}-\d{2}-\d{2}$"))
def handle_confirm_action(ack, body, client, logger):
    """Handle 確定 button — read selected start/end times from state."""
    ack()

    action = body["actions"][0]
    action_id = action["action_id"]
    user_id = body["user"]["id"]
    user_name = body["user"].get("name", user_id)

    shift_date = action_id.replace("confirm_", "")
    staff_id = action.get("value", "")

    # Read static_select values from state
    state_values = body.get("state", {}).get("values", {})
    actions_block = state_values.get(f"actions_{shift_date}", {})
    start_obj = actions_block.get(f"start_{shift_date}", {}).get("selected_option")
    end_obj = actions_block.get(f"end_{shift_date}", {}).get("selected_option")
    start_time = start_obj["value"] if start_obj else "10:00"
    end_time = end_obj["value"] if end_obj else "19:00"

    logger.info(f"Confirm: {staff_id} / {shift_date} / 出勤 {start_time}-{end_time} by {user_name}")

    success = upsert_kintone_shift(staff_id, shift_date, "出勤", start_time, end_time)

    if not success:
        client.chat_postMessage(
            channel=user_id,
            text=f"❌ kintone の更新に失敗しました。({shift_date} / 出勤 {start_time}-{end_time})\n管理者に連絡してください。",
        )
        return

    new_blocks = replace_date_blocks(
        body["message"]["blocks"],
        shift_date,
        build_confirmed_blocks(shift_date, staff_id, "出勤", start_time, end_time),
    )
    update_message(client, body, new_blocks,
                   f"シフト希望入力（{shift_date}: 出勤 {start_time}-{end_time}）")


# Time select change events — just ack, value is read on confirm
@bolt_app.action(re.compile(r"^(start|end)_\d{4}-\d{2}-\d{2}$"))
def handle_time_select_change(ack, body, logger):
    """Ack time dropdown changes (state is read on confirm)."""
    ack()


# Edit button: return to initial [出勤] [休み] [希望休] from confirmed state
@bolt_app.action(re.compile(r"^edit_\d{4}-\d{2}-\d{2}$"))
def handle_edit_action(ack, body, client, logger):
    """Handle 修正 button to return to step 1 from confirmed state."""
    ack()

    action = body["actions"][0]
    shift_date = action["action_id"].replace("edit_", "")
    staff_id = action.get("value", "")

    new_blocks = replace_date_blocks(
        body["message"]["blocks"],
        shift_date,
        build_initial_buttons(shift_date, staff_id),
    )
    update_message(client, body, new_blocks, "シフト希望入力")


# Back button: return to initial [出勤] [休み] [希望休]
@bolt_app.action(re.compile(r"^back_\d{4}-\d{2}-\d{2}$"))
def handle_back_action(ack, body, client, logger):
    """Handle 戻る button to return to step 1."""
    ack()

    action = body["actions"][0]
    action_id = action["action_id"]
    shift_date = action_id.replace("back_", "")
    staff_id = action.get("value", "")

    new_blocks = replace_date_blocks(
        body["message"]["blocks"],
        shift_date,
        build_initial_buttons(shift_date, staff_id),
    )
    update_message(client, body, new_blocks, "シフト希望入力")


# -----------------------------------------------------------------------
# Fixed-shift staff: 希望休 input handlers
# -----------------------------------------------------------------------

def _parse_fixed_value(value_str):
    """Parse 'S001|2026-03-16|2026-03-29' from button value."""
    parts = value_str.split("|")
    if len(parts) == 3:
        return parts[0], parts[1], parts[2]
    return parts[0] if parts else "", "", ""


def build_datepicker_blocks(count, staff_id, period_start, period_end):
    """Build datepicker blocks for N days off."""
    blocks = [
        {
            "type": "section",
            "block_id": "fixed_header",
            "text": {
                "type": "mrkdwn",
                "text": f"*希望休 {count}日分* の日付を選択して「送信」を押してください。",
            },
        },
    ]

    # Group datepickers into actions blocks (max 5 elements per block)
    pickers = []
    for i in range(1, count + 1):
        pickers.append({
            "type": "datepicker",
            "action_id": f"fixeddate_{i}",
            "placeholder": {"type": "plain_text", "text": f"{i}日目"},
        })

    # Chunk into groups of 4 (leave room for buttons in last group)
    chunk_size = 4
    for idx in range(0, len(pickers), chunk_size):
        chunk = pickers[idx:idx + chunk_size]
        blocks.append({
            "type": "actions",
            "block_id": f"fixed_dates_{idx // chunk_size}",
            "elements": chunk,
        })

    # Submit + back buttons
    blocks.append({
        "type": "actions",
        "block_id": "fixed_submit_actions",
        "elements": [
            {
                "type": "button",
                "text": {"type": "plain_text", "text": "📨 送信"},
                "style": "primary",
                "value": f"{staff_id}|{period_start}|{period_end}",
                "action_id": "fixedsubmit",
            },
            {
                "type": "button",
                "text": {"type": "plain_text", "text": "↩️ 戻る"},
                "value": f"{staff_id}|{period_start}|{period_end}",
                "action_id": "fixedback",
            },
        ],
    })

    return blocks


def build_fixed_confirmed_blocks(staff_id, dates, period_start, period_end):
    """Build confirmed blocks for fixed-shift 希望休."""
    if dates:
        date_list = "、".join(dates)
        text = f"✅ *希望休 {len(dates)}日* を登録しました。\n{date_list}"
    else:
        text = "✅ *希望休なし* で登録しました。"

    return [
        {
            "type": "section",
            "block_id": "fixed_confirmed",
            "text": {"type": "mrkdwn", "text": text},
            "accessory": {
                "type": "button",
                "text": {"type": "plain_text", "text": "✏️ 修正"},
                "value": f"{staff_id}|{period_start}|{period_end}",
                "action_id": "fixededit",
            },
        },
    ]


def rebuild_fixed_initial(staff_id, period_start, period_end, deadline):
    """Rebuild the initial fixed-shift message blocks."""
    from slack_shift_bot import build_fixed_shift_blocks
    return build_fixed_shift_blocks(staff_id, "", period_start, period_end, deadline)


def replace_all_content_blocks(original_blocks, new_content):
    """Replace all blocks between header and footer context with new content."""
    header_blocks = []
    footer_blocks = []
    found_divider_count = 0

    for block in original_blocks:
        if block.get("type") == "header":
            header_blocks.append(block)
            continue
        if block.get("type") == "section" and "fields" in block:
            header_blocks.append(block)
            continue
        if block.get("type") == "divider":
            found_divider_count += 1
            if found_divider_count == 1:
                header_blocks.append(block)
                continue
        if block.get("type") == "context":
            footer_blocks.append(block)
            continue

    # If we couldn't parse, just use new content
    if not header_blocks:
        return new_content

    return header_blocks + new_content + footer_blocks


# Handler: day count selected
@bolt_app.action("fixedcount")
def handle_fixed_count(ack, body, client, logger):
    """Handle day count selection for fixed-shift staff."""
    ack()

    action = body["actions"][0]
    count = int(action.get("selected_option", {}).get("value", "1"))
    user_id = body["user"]["id"]

    # Find staff_id and period from the "希望休なし" button in the same message
    staff_id, period_start, period_end = "", "", ""
    for block in body["message"]["blocks"]:
        for elem in block.get("elements", []):
            if elem.get("action_id") == "fixednone":
                staff_id, period_start, period_end = _parse_fixed_value(elem.get("value", ""))
                break

    logger.info(f"Fixed count: {count} days by {user_id} ({staff_id})")

    new_content = build_datepicker_blocks(count, staff_id, period_start, period_end)
    new_blocks = replace_all_content_blocks(body["message"]["blocks"], new_content)

    update_message(client, body, new_blocks, f"希望休入力（{count}日選択中）")


# Handler: 希望休なし
@bolt_app.action("fixednone")
def handle_fixed_none(ack, body, client, logger):
    """Handle 希望休なし button."""
    ack()

    action = body["actions"][0]
    staff_id, period_start, period_end = _parse_fixed_value(action.get("value", ""))
    user_id = body["user"]["id"]
    user_name = body["user"].get("name", user_id)

    logger.info(f"Fixed none: {staff_id} by {user_name}")

    new_content = build_fixed_confirmed_blocks(staff_id, [], period_start, period_end)
    new_blocks = replace_all_content_blocks(body["message"]["blocks"], new_content)
    update_message(client, body, new_blocks, "希望休なし登録済み")


# Handler: datepicker changes — just ack
@bolt_app.action(re.compile(r"^fixeddate_\d+$"))
def handle_fixed_date_change(ack, body, logger):
    """Ack datepicker changes."""
    ack()


# Handler: 送信
@bolt_app.action("fixedsubmit")
def handle_fixed_submit(ack, body, client, logger):
    """Handle submit of selected dates."""
    ack()

    action = body["actions"][0]
    staff_id, period_start, period_end = _parse_fixed_value(action.get("value", ""))
    user_id = body["user"]["id"]
    user_name = body["user"].get("name", user_id)

    # Read all datepicker values from state
    state_values = body.get("state", {}).get("values", {})
    selected_dates = []
    for block_id, block_state in state_values.items():
        if not block_id.startswith("fixed_dates_"):
            continue
        for action_id, action_state in block_state.items():
            if action_id.startswith("fixeddate_"):
                date_val = action_state.get("selected_date")
                if date_val:
                    selected_dates.append(date_val)

    selected_dates.sort()

    if not selected_dates:
        client.chat_postMessage(
            channel=user_id,
            text="⚠️ 日付が選択されていません。カレンダーから日付を選んでください。",
        )
        return

    logger.info(f"Fixed submit: {staff_id} / {selected_dates} by {user_name}")

    # Save each date to kintone as 希望休
    all_ok = True
    for date_str in selected_dates:
        ok = upsert_kintone_shift(staff_id, date_str, "希望休")
        if not ok:
            all_ok = False

    if not all_ok:
        client.chat_postMessage(
            channel=user_id,
            text=f"⚠️ 一部の日付でkintone登録に失敗しました。管理者に連絡してください。",
        )

    new_content = build_fixed_confirmed_blocks(staff_id, selected_dates, period_start, period_end)
    new_blocks = replace_all_content_blocks(body["message"]["blocks"], new_content)
    update_message(client, body, new_blocks,
                   f"希望休 {len(selected_dates)}日登録済み")


# Handler: 戻る (datepicker → count selection)
@bolt_app.action("fixedback")
def handle_fixed_back(ack, body, client, logger):
    """Handle back button to return to count selection."""
    ack()

    action = body["actions"][0]
    staff_id, period_start, period_end = _parse_fixed_value(action.get("value", ""))
    deadline = (datetime.datetime.strptime(period_start, "%Y-%m-%d") - datetime.timedelta(days=1)).strftime("%Y-%m-%d")

    new_blocks = rebuild_fixed_initial(staff_id, period_start, period_end, deadline)
    update_message(client, body, new_blocks, "希望休入力")


# Handler: 修正 (confirmed → count selection)
@bolt_app.action("fixededit")
def handle_fixed_edit(ack, body, client, logger):
    """Handle edit button to re-enter from confirmed state."""
    ack()

    action = body["actions"][0]
    staff_id, period_start, period_end = _parse_fixed_value(action.get("value", ""))
    deadline = (datetime.datetime.strptime(period_start, "%Y-%m-%d") - datetime.timedelta(days=1)).strftime("%Y-%m-%d")

    new_blocks = rebuild_fixed_initial(staff_id, period_start, period_end, deadline)
    update_message(client, body, new_blocks, "希望休入力")


# -----------------------------------------------------------------------
# Auto post-approval trigger (from GAS approval)
# -----------------------------------------------------------------------

@bolt_app.message(re.compile(r"\[auto-post-approval\]\s+(\S+)"))
def handle_auto_post_approval(message, say, logger, context):
    """Detect GAS approval trigger message and run post-approval automatically."""
    import subprocess

    match = context["matches"][0] if context.get("matches") else None
    if not match:
        return

    schedule_version = match.group(1) if hasattr(match, 'group') else match
    # Extract version from regex match
    text = message.get("text", "")
    m = re.search(r"\[auto-post-approval\]\s+(\S+)", text)
    if not m:
        return
    schedule_version = m.group(1)

    logger.info(f"Auto post-approval triggered: {schedule_version}")

    say(
        text=f"⚙️ 後続処理を開始します... (version: {schedule_version})",
        thread_ts=message["ts"],
    )

    # Run run_post_approval.py in background
    script_dir = os.path.dirname(os.path.abspath(__file__))
    cmd = [sys.executable, os.path.join(script_dir, "run_post_approval.py"), schedule_version]

    try:
        result = subprocess.run(
            cmd,
            cwd=script_dir,
            capture_output=True,
            text=True,
            timeout=300,
        )
        output = result.stdout[-1500:] if result.stdout else ""
        errors = result.stderr[-500:] if result.stderr else ""

        if result.returncode == 0:
            say(
                text=f"✅ 後続処理が完了しました。\n```\n{output}\n```",
                thread_ts=message["ts"],
            )
        else:
            say(
                text=f"⚠️ 後続処理でエラーが発生しました (code={result.returncode}):\n```\n{output}\n{errors}\n```",
                thread_ts=message["ts"],
            )
    except subprocess.TimeoutExpired:
        say(
            text="❌ 後続処理がタイムアウトしました（5分超過）。手動で確認してください。",
            thread_ts=message["ts"],
        )
    except Exception as e:
        say(
            text=f"❌ 後続処理の実行に失敗しました: {e}",
            thread_ts=message["ts"],
        )


if __name__ == "__main__":
    logger.info("Starting Bolt server (Socket Mode)...")
    socket_handler = SocketModeHandler(bolt_app, SLACK_APP_TOKEN)
    socket_handler.start()
