#!/usr/bin/env python3
"""STEP F: Shift start reminder.

Sends reminders to staff before their shift period starts.
Checks kintone app 212 for confirmed shifts and notifies via Slack DM and LINE.

Usage:
    python3 reminder.py                                # dry-run, 3 days ahead
    python3 reminder.py --no-dry-run                   # live send
    python3 reminder.py --days 5                       # 5 days ahead
    python3 reminder.py --date 2026-04-01              # override today's date
"""
import base64
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta

from config import (
    KINTONE_DOMAIN, KINTONE_USERNAME, KINTONE_PASSWORD,
    KINTONE_SHIFT_CONFIRMED_APP_ID, SHIFT_SPREADSHEET_ID,
    SLACK_BOT_TOKEN, SLACK_SHIFT_CHANNEL,
    LINE_CHANNEL_ACCESS_TOKEN,
)

KINTONE_APP_URL = f"https://{KINTONE_DOMAIN}/k/{KINTONE_SHIFT_CONFIRMED_APP_ID}/"


# ---------------------------------------------------------------------------
# kintone API
# ---------------------------------------------------------------------------

def _kintone_headers():
    credential = base64.b64encode(
        f"{KINTONE_USERNAME}:{KINTONE_PASSWORD}".encode()
    ).decode()
    return {
        "X-Cybozu-Authorization": credential,
        "Content-Type": "application/json",
    }


def fetch_confirmed_shifts():
    """Fetch all confirmed shift records from kintone ID=212.

    Returns list of dicts: {staff_id, staff_name, shift_period, working_days}
    """
    headers = _kintone_headers()
    query = 'order by staff_id asc'
    encoded_query = urllib.parse.quote(query)
    url = (
        f"https://{KINTONE_DOMAIN}/k/v1/records.json"
        f"?app={KINTONE_SHIFT_CONFIRMED_APP_ID}"
        f"&query={encoded_query}&totalCount=true"
    )
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req) as resp:
        data = json.loads(resp.read())

    records = data.get("records", [])
    result = []
    for r in records:
        staff_id = r.get("staff_id", {}).get("value", "")
        staff_name = r.get("staff_name", {}).get("value", "")
        shift_period = r.get("shift_period", {}).get("value", "")
        working_days_str = r.get("working_days", {}).get("value", "")
        working_days = [d.strip() for d in working_days_str.split(",") if d.strip()]

        result.append({
            "staff_id": staff_id,
            "staff_name": staff_name,
            "shift_period": shift_period,
            "working_days": working_days,
        })

    return result


def extract_start_date(shift_period):
    """Extract start date from shift_period string.

    Examples:
        "2026-03-09 ~ 2026-03-22" -> "2026-03-09"
        "2026-03-09_2026-03-22" -> "2026-03-09"
    """
    if not shift_period:
        return None
    # Try "~" separator first
    if "~" in shift_period:
        return shift_period.split("~")[0].strip()
    # Try "_" separator
    if "_" in shift_period:
        parts = shift_period.split("_")
        # Could be "2026-03-09_2026-03-22" or "2026-03-09_v1"
        if len(parts) >= 2 and len(parts[0]) == 10:
            return parts[0]
    # Assume the whole string is a date
    if len(shift_period) == 10:
        return shift_period
    return None


# ---------------------------------------------------------------------------
# Google Sheets API (staff master)
# ---------------------------------------------------------------------------

def _find_google_credential():
    import os
    base_dir = os.path.expanduser("~/.google_workspace_mcp")
    for subdir in ["chillaxy", "gw-chillaxy"]:
        target = os.path.join(base_dir, subdir, "satoru@chillaxy.jp.json")
        if os.path.exists(target):
            return target
    if os.path.isdir(base_dir):
        for subdir in os.listdir(base_dir):
            path = os.path.join(base_dir, subdir, "satoru@chillaxy.jp.json")
            if os.path.exists(path):
                return path
    return None


def _get_access_token(cred_path):
    with open(cred_path) as f:
        cred = json.load(f)

    token = cred.get("token") or cred.get("access_token")
    expiry = cred.get("expiry") or cred.get("token_expiry")

    if token and expiry:
        try:
            exp_dt = datetime.fromisoformat(expiry.replace("Z", "+00:00"))
            if exp_dt > datetime.now(exp_dt.tzinfo):
                return token
        except (ValueError, TypeError):
            pass

    refresh_token = cred.get("refresh_token")
    client_id = cred.get("client_id")
    client_secret = cred.get("client_secret")

    if refresh_token and client_id and client_secret:
        url = "https://oauth2.googleapis.com/token"
        params = urllib.parse.urlencode({
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": client_id,
            "client_secret": client_secret,
        }).encode()
        req = urllib.request.Request(url, data=params, method="POST")
        with urllib.request.urlopen(req) as resp:
            result = json.loads(resp.read())
        new_token = result["access_token"]

        cred["token"] = new_token
        cred["access_token"] = new_token
        expires_in = result.get("expires_in", 3600)
        new_expiry = (datetime.utcnow() + timedelta(seconds=expires_in)).isoformat() + "Z"
        cred["expiry"] = new_expiry
        cred["token_expiry"] = new_expiry
        with open(cred_path, "w") as f:
            json.dump(cred, f, indent=2)

        return new_token

    if token:
        return token

    raise RuntimeError(f"Cannot get access token from {cred_path}")


def sheets_read(range_name):
    cred_path = _find_google_credential()
    if not cred_path:
        raise RuntimeError("No Google OAuth credential found")
    token = _get_access_token(cred_path)
    encoded_range = urllib.parse.quote(range_name)
    url = (
        f"https://sheets.googleapis.com/v4/spreadsheets/{SHIFT_SPREADSHEET_ID}"
        f"/values/{encoded_range}"
    )
    headers = {"Authorization": f"Bearer {token}"}
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req) as resp:
        data = json.loads(resp.read())
    return data.get("values", [])


def read_staff_master():
    """Read staff master keyed by staff_id."""
    rows = sheets_read("スタッフマスタ!A1:I100")
    if not rows:
        return {}

    header = rows[0]
    staff_map = {}
    for row in rows[1:]:
        if not row or not row[0]:
            continue
        row += [""] * (len(header) - len(row))
        d = {}
        for i, h in enumerate(header):
            d[h] = row[i]
        if d.get("有効フラグ", "").upper() in ("FALSE", "0", "無効"):
            continue
        staff_id = d.get("staff_id", "")
        if staff_id:
            staff_map[staff_id] = d

    return staff_map


# ---------------------------------------------------------------------------
# Messaging
# ---------------------------------------------------------------------------

def build_reminder_message(staff_name, start_date, working_days):
    """Build reminder message for a staff member."""
    days_list = ", ".join(working_days) if working_days else "なし"
    return (
        f"【リマインド】{start_date}からのシフトが始まります。\n\n"
        f"あなたのシフト:\n"
        f"出勤日: {days_list}\n\n"
        f"詳細はkintoneから確認できます:\n"
        f"{KINTONE_APP_URL}"
    )


def send_slack_dm(slack_id, message):
    """Send a DM to a staff member via Slack."""
    if not SLACK_BOT_TOKEN:
        print(f"  [Slack skip] No token. Would DM {slack_id}")
        return False

    # Open DM conversation
    url = "https://slack.com/api/conversations.open"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {SLACK_BOT_TOKEN}",
    }
    body = {"users": slack_id}
    data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, headers=headers)
    with urllib.request.urlopen(req) as resp:
        dm = json.loads(resp.read())

    if not dm.get("ok"):
        raise RuntimeError(f"Cannot open DM with {slack_id}: {dm.get('error')}")

    channel_id = dm["channel"]["id"]

    # Send message
    url = "https://slack.com/api/chat.postMessage"
    body = {"channel": channel_id, "text": message}
    data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, headers=headers)
    with urllib.request.urlopen(req) as resp:
        result = json.loads(resp.read())

    if not result.get("ok"):
        raise RuntimeError(f"DM send failed: {result.get('error')}")
    return True


def send_line_push(line_uid, message):
    """Send a LINE push message."""
    if not LINE_CHANNEL_ACCESS_TOKEN:
        print(f"  [LINE skip] No token. Would push to {line_uid}")
        return False

    url = "https://api.line.me/v2/bot/message/push"
    body = {
        "to": line_uid,
        "messages": [{"type": "text", "text": message}],
    }
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}",
    }
    data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, headers=headers)
    with urllib.request.urlopen(req) as resp:
        json.loads(resp.read())
    return True


# ---------------------------------------------------------------------------
# Slack admin notification
# ---------------------------------------------------------------------------

def slack_notify(text):
    if not SLACK_BOT_TOKEN:
        print(f"[Slack skip] {text}")
        return
    url = "https://slack.com/api/chat.postMessage"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {SLACK_BOT_TOKEN}",
    }
    body = {"channel": SLACK_SHIFT_CHANNEL, "text": text}
    data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, headers=headers)
    try:
        with urllib.request.urlopen(req) as resp:
            resp.read()
    except Exception as e:
        print(f"Slack notify failed: {e}")


def slack_notify_error(location, error):
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    slack_notify(f"【エラー通知】発生箇所:{location} エラー:{error} 日時:{now}")


# ---------------------------------------------------------------------------
# Main logic
# ---------------------------------------------------------------------------

def run(dry_run=True, days_ahead=3, reference_date=None):
    """Run shift reminder check.

    Args:
        dry_run: If True, only print what would be sent
        days_ahead: How many days before shift start to send reminder
        reference_date: Override today's date (YYYY-MM-DD string)
    """
    print("=== Shift Reminder (STEP F) ===\n")

    if reference_date:
        today = datetime.strptime(reference_date, "%Y-%m-%d").date()
    else:
        today = datetime.now().date()

    target_start = today + timedelta(days=days_ahead)
    target_start_str = target_start.strftime("%Y-%m-%d")

    print(f"  Today: {today}")
    print(f"  Days ahead: {days_ahead}")
    print(f"  Looking for shifts starting on: {target_start_str}")
    print(f"  Mode: {'dry-run' if dry_run else 'LIVE'}")

    try:
        # (1) Fetch confirmed shifts from kintone
        print("\n[1/3] Fetching confirmed shifts from kintone...")
        all_records = fetch_confirmed_shifts()
        print(f"  Total records: {len(all_records)}")

        # (2) Filter to shifts starting on target date
        target_records = []
        for r in all_records:
            start_date = extract_start_date(r["shift_period"])
            if start_date == target_start_str:
                target_records.append(r)

        print(f"  Matching records (start={target_start_str}): {len(target_records)}")

        if not target_records:
            print("\n  No shifts starting on target date. Nothing to send.")
            return target_records

        # (3) Get staff contact info
        print("\n[2/3] Reading staff master...")
        staff_map = read_staff_master()
        print(f"  Staff master: {len(staff_map)} entries")

        # (4) Send reminders
        print("\n[3/3] Sending reminders...")
        slack_sent = 0
        line_sent = 0
        errors = []

        for r in target_records:
            staff_id = r["staff_id"]
            staff_name = r["staff_name"]
            staff_info = staff_map.get(staff_id, {})
            slack_id = staff_info.get("Slack ID", "").strip()
            line_uid = staff_info.get("LINE UID", "").strip()

            message = build_reminder_message(
                staff_name, target_start_str, r["working_days"]
            )

            if dry_run:
                print(f"  [dry-run] {staff_id} {staff_name}:")
                if slack_id:
                    print(f"    Slack DM -> {slack_id}")
                if line_uid:
                    print(f"    LINE -> {line_uid}")
                if not slack_id and not line_uid:
                    print(f"    (no contact info)")
                continue

            # Slack DM
            if slack_id:
                try:
                    send_slack_dm(slack_id, message)
                    slack_sent += 1
                    print(f"  [Slack DM] {staff_name}")
                except Exception as e:
                    error = f"Slack DM {staff_name} ({slack_id}): {e}"
                    errors.append(error)
                    print(f"  [ERROR] {error}")

            # LINE push
            if line_uid:
                try:
                    send_line_push(line_uid, message)
                    line_sent += 1
                    print(f"  [LINE] {staff_name}")
                except Exception as e:
                    error = f"LINE {staff_name} ({line_uid}): {e}"
                    errors.append(error)
                    print(f"  [ERROR] {error}")

            if not slack_id and not line_uid:
                print(f"  [SKIP] {staff_name}: No contact info")

        if not dry_run:
            print(f"\n  Sent: Slack DM={slack_sent}, LINE={line_sent}")
            if errors:
                slack_notify_error("reminder", "\n".join(errors))
            else:
                slack_notify(
                    f"シフトリマインド送信完了\n"
                    f"対象開始日: {target_start_str}\n"
                    f"対象スタッフ: {len(target_records)}名\n"
                    f"Slack DM: {slack_sent}件 / LINE: {line_sent}件"
                )

        print(f"\n=== Reminder {'preview' if dry_run else 'complete'} ===")
        return target_records

    except Exception as e:
        error_msg = str(e)
        print(f"\nERROR: {error_msg}")
        slack_notify_error("reminder", error_msg)
        raise


def main():
    if "--help" in sys.argv:
        print(__doc__)
        return

    dry_run = "--no-dry-run" not in sys.argv
    days_ahead = 3
    reference_date = None

    args = sys.argv[1:]
    for i, arg in enumerate(args):
        if arg == "--days" and i + 1 < len(args):
            days_ahead = int(args[i + 1])
        elif arg == "--date" and i + 1 < len(args):
            reference_date = args[i + 1]

    run(dry_run=dry_run, days_ahead=days_ahead, reference_date=reference_date)


if __name__ == "__main__":
    main()
