#!/usr/bin/env python3
"""STEP 8: Post-approval shift notification.

After shift approval, notifies all staff via:
  - Slack channel: full shift announcement
  - Slack DM: individual schedule per staff (by Slack ID)
  - LINE: individual schedule per staff (by LINE UID)

Usage:
    python3 notify_shift.py                          # Auto-read from sheet
    python3 notify_shift.py 2026-03-09 2026-03-22   # Specify period
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

GOOGLE_EMAIL = "satoru@chillaxy.jp"
KINTONE_APP_URL = f"https://{KINTONE_DOMAIN}/k/{KINTONE_SHIFT_CONFIRMED_APP_ID}/"


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


def line_push(user_id, message):
    if not LINE_CHANNEL_ACCESS_TOKEN:
        print(f"  [LINE skip] No token. Would send to {user_id}")
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


def slack_notify_error(location, error):
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    text = f"【エラー通知】発生箇所:{location} エラー:{error} 日時:{now}"
    if not SLACK_BOT_TOKEN:
        print(f"[Slack skip] {text}")
        return
    try:
        slack_api("chat.postMessage", json_body={
            "channel": SLACK_SHIFT_CHANNEL,
            "text": text,
        })
    except Exception as e:
        print(f"Slack error notify failed: {e}")


# ---------------------------------------------------------------------------
# Google Sheets helpers (reuse from generate_shift.py)
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


# ---------------------------------------------------------------------------
# Read data from spreadsheet
# ---------------------------------------------------------------------------

def read_shift_output():
    """Read shift data from 'シフト出力' sheet (store-based format).

    Layout:
      Row 1: metadata (シフト期間 + year/month dropdowns)
      Row 2: store group headers
      Row 3: sub-headers (早番, 遅番, ..., staff_names)
      Row 4: label row (確定, 変更, blank, 休日) + rest day counts
      Row 5+: data rows (FALSE, FALSE, M/D, dow, name, name, ..., time, time, ...)

    Returns (period, approver, approval_time, schedule_version, dates, shift_data).
    shift_data is list of dicts per staff with work/off info and time schedules.
    """
    rows = sheets_read("シフト出力!A1:BZ200")
    if len(rows) < 5:
        raise RuntimeError("シフト出力シートにデータがありません")

    # Row 1: metadata
    period = rows[0][1] if len(rows[0]) > 1 else ""
    approver = ""
    approval_time = ""
    schedule_version = ""

    # Row 3 (index 2): sub-headers with staff names
    sub_header = rows[2] if len(rows) > 2 else []
    # Row 4 (index 3): label row (確定/変更/blank/休日) - skip

    # Identify individual staff columns (not system/store labels)
    system_labels = {"確定", "変更", "", "早番", "遅番", "休日"}
    staff_columns = {}  # col_idx -> staff_name
    for i, label in enumerate(sub_header):
        if label and label not in system_labels:
            staff_columns[i] = label

    # Extract dates from data rows (column C = index 2), starting at row 5 (index 4)
    data_rows = rows[4:]
    dates = []
    # Parse year from period string "YYYY-MM-DD ~ YYYY-MM-DD"
    year = period.split("-")[0].strip() if period and "-" in period else str(datetime.now().year)
    for row in data_rows:
        if len(row) > 2 and row[2]:
            date_label = str(row[2]).strip()
            if "/" in date_label:
                parts = date_label.split("/")
                try:
                    dates.append(f"{year}-{int(parts[0]):02d}-{int(parts[1]):02d}")
                except (ValueError, IndexError):
                    dates.append(date_label)

    # Build per-staff data from individual time columns
    shift_data = []
    for col_idx, staff_name in staff_columns.items():
        work_days = []
        off_days = []
        times_map = {}

        for row_idx, row in enumerate(data_rows):
            if row_idx >= len(dates):
                break
            date = dates[row_idx]
            time_val = row[col_idx] if col_idx < len(row) else ""

            if time_val:
                work_days.append(date)
                times_map[date] = time_val
            else:
                off_days.append(date)

        shift_data.append({
            "staff_id": "",  # looked up from staff_master later
            "name": staff_name,
            "schedule": [],
            "work_days": work_days,
            "off_days": off_days,
            "wish_off_days": [],
            "total_work": str(len(work_days)),
            "total_hours": f"{len(work_days) * 8}h",
            "times": times_map,
        })

    return period, approver, approval_time, schedule_version, dates, shift_data


def read_staff_master():
    """Read staff master keyed by staff_id. Also builds name->staff_id map."""
    rows = sheets_read("スタッフマスタ!A1:Q100")
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
# Build notification messages
# ---------------------------------------------------------------------------

def build_channel_message(period, approver, approval_time, shift_data):
    """Build Slack channel announcement message."""
    summary_lines = []
    for s in shift_data:
        summary_lines.append(
            f"  {s['name']}: 出勤{s['total_work']}日 ({s['total_hours']})"
        )

    text = (
        f"*シフト確定のお知らせ*\n\n"
        f"対象期間: *{period}*\n"
        f"承認者: {approver}\n"
        f"承認日時: {approval_time}\n\n"
        f"*各スタッフの出勤日数:*\n```\n"
        + "\n".join(summary_lines)
        + f"\n```\n\n"
        f"各自のシフト詳細はDMをご確認ください。\n"
        f"kintone: {KINTONE_APP_URL}"
    )
    return text


def build_individual_message(staff_entry, period):
    """Build individual notification message for a staff member."""
    s = staff_entry
    times_map = s.get("times", {})

    # Build work schedule with times
    work_lines = []
    dow_names = ["月", "火", "水", "木", "金", "土", "日"]
    for date in s["work_days"]:
        try:
            d = datetime.strptime(date, "%Y-%m-%d")
            dow = dow_names[d.weekday()]
            short_date = f"{d.month}/{d.day}({dow})"
        except ValueError:
            short_date = date
        time_str = times_map.get(date, "")
        if time_str:
            work_lines.append(f"  {short_date} {time_str}")
        else:
            work_lines.append(f"  {short_date}")

    off_lines = []
    for date in s["off_days"]:
        try:
            d = datetime.strptime(date, "%Y-%m-%d")
            dow = dow_names[d.weekday()]
            off_lines.append(f"  {d.month}/{d.day}({dow})")
        except ValueError:
            off_lines.append(f"  {date}")

    text = (
        f"シフト確定通知\n\n"
        f"{s['name']}さん\n\n"
        f"対象期間: {period}\n\n"
        f"出勤日 ({s['total_work']}日):\n"
        + ("\n".join(work_lines) if work_lines else "  なし")
        + f"\n\n休日:\n"
        + ("\n".join(off_lines) if off_lines else "  なし")
        + "\n"
    )

    text += f"\nkintone: {KINTONE_APP_URL}"

    return text


# ---------------------------------------------------------------------------
# Send notifications
# ---------------------------------------------------------------------------

def send_slack_channel_announcement(period, approver, approval_time, shift_data):
    """Post shift confirmation to Slack channel."""
    text = build_channel_message(period, approver, approval_time, shift_data)

    result = slack_api("chat.postMessage", json_body={
        "channel": SLACK_SHIFT_CHANNEL,
        "text": text,
        "mrkdwn": True,
    })

    if result.get("ok"):
        print(f"  Slack channel announcement posted")
    else:
        raise RuntimeError(f"Slack channel post failed: {result.get('error')}")


def send_slack_dm(slack_id, message):
    """Send a DM to a staff member via Slack."""
    # Open DM conversation
    dm = slack_api("conversations.open", json_body={"users": slack_id})
    if not dm.get("ok"):
        raise RuntimeError(f"Cannot open DM with {slack_id}: {dm.get('error')}")

    channel_id = dm["channel"]["id"]
    result = slack_api("chat.postMessage", json_body={
        "channel": channel_id,
        "text": message,
    })

    if not result.get("ok"):
        raise RuntimeError(f"DM send failed for {slack_id}: {result.get('error')}")


def send_individual_notifications(shift_data, staff_map, period):
    """Send individual notifications to all staff via Slack DM and LINE."""
    slack_sent = 0
    line_sent = 0
    errors = []

    for s in shift_data:
        name = s["name"]
        sid = s.get("staff_id", "")
        staff_info = staff_map.get(sid, {})
        slack_id = staff_info.get("Slack ID", "").strip()
        line_uid = staff_info.get("LINE UID", "").strip()

        message = build_individual_message(s, period)

        # Slack DM
        if slack_id:
            try:
                send_slack_dm(slack_id, message)
                slack_sent += 1
                print(f"  [Slack DM] {name}")
            except Exception as e:
                error = f"Slack DM to {name} ({slack_id}): {e}"
                errors.append(error)
                print(f"  [ERROR] {error}")

        # LINE push
        if line_uid:
            try:
                line_push(line_uid, message)
                line_sent += 1
                print(f"  [LINE] {name}")
            except Exception as e:
                error = f"LINE to {name} ({line_uid}): {e}"
                errors.append(error)
                print(f"  [ERROR] {error}")

        if not slack_id and not line_uid:
            print(f"  [SKIP] {name}: No Slack ID or LINE UID configured")

    print(f"\n  Sent: Slack DM={slack_sent}, LINE={line_sent}")

    if errors:
        error_summary = "\n".join(errors)
        slack_notify_error("notify_shift/individual", error_summary)

    return errors


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run(schedule_version=None):
    """Main notification flow with sync_outbox integration."""
    import sync_outbox

    print("=== Shift Notification (STEP 8) ===\n")

    try:
        # Read shift output
        print("[1/3] Reading shift data from spreadsheet...")
        period, approver, approval_time, sv, dates, shift_data = read_shift_output()
        version = schedule_version or sv
        print(f"  Period: {period}")
        print(f"  Approver: {approver}")
        print(f"  Schedule version: {version}")
        print(f"  Staff count: {len(shift_data)}")

        # Initialize outbox if version exists but not yet tracked
        if version and not sync_outbox.get_status(version):
            period_start = period.split("~")[0].strip() if "~" in period else period
            sync_outbox.create_version(period_start, approver)

        # Read staff master early (needed for staff_id lookup and notifications)
        print("[2/3] Reading staff master...")
        staff_map = read_staff_master()
        print(f"  Staff master: {len(staff_map)} entries")

        # Resolve staff_id for shift_data entries (new format doesn't have it in sheet)
        name_to_sid = {v.get("氏名", ""): k for k, v in staff_map.items()}
        for s in shift_data:
            if not s["staff_id"] and s["name"]:
                s["staff_id"] = name_to_sid.get(s["name"], "")

        # Check if slack destination should run
        if version and not sync_outbox.should_run(version, "slack"):
            print("  Slack already sent for this version, skipping.")

            # Send Slack notifications
            print("[3/3] Sending Slack notifications...")
            try:
                print("  Posting channel announcement...")
                send_slack_channel_announcement(period, approver, approval_time, shift_data)
                print("  Sending individual Slack DMs...")
                slack_errors = []
                line_errors = []
                for s in shift_data:
                    name = s["name"]
                    sid = s.get("staff_id", "")
                    staff_info = staff_map.get(sid, {})
                    slack_id = staff_info.get("Slack ID", "").strip()
                    message = build_individual_message(s, period)
                    if slack_id:
                        try:
                            send_slack_dm(slack_id, message)
                            print(f"  [Slack DM] {name}")
                        except Exception as e:
                            slack_errors.append(f"{name}: {e}")
                            print(f"  [ERROR] Slack DM {name}: {e}")

                if slack_errors:
                    if version:
                        sync_outbox.update_status(version, "slack", "partial_failed",
                                                  f"{len(slack_errors)} DM failures")
                else:
                    if version:
                        sync_outbox.update_status(version, "slack", "sent")
            except Exception as e:
                if version:
                    sync_outbox.update_status(version, "slack", "failed", str(e))
                raise

        # Send LINE notifications
        if version and not sync_outbox.should_run(version, "line"):
            print("  LINE already sent for this version, skipping.")
        else:
            print("  Sending LINE notifications...")
            if staff_map is None:
                staff_map = read_staff_master()
            try:
                line_errors = []
                for s in shift_data:
                    name = s["name"]
                    sid = s.get("staff_id", "")
                    staff_info = staff_map.get(sid, {})
                    line_uid = staff_info.get("LINE UID", "").strip()
                    if line_uid:
                        message = build_individual_message(s, period)
                        try:
                            line_push(line_uid, message)
                            print(f"  [LINE] {name}")
                        except Exception as e:
                            line_errors.append(f"{name}: {e}")
                            print(f"  [ERROR] LINE {name}: {e}")

                if line_errors:
                    if version:
                        sync_outbox.update_status(version, "line", "partial_failed",
                                                  f"{len(line_errors)} failures")
                else:
                    if version:
                        sync_outbox.update_status(version, "line", "sent")
            except Exception as e:
                if version:
                    sync_outbox.update_status(version, "line", "failed", str(e))
                raise

        print(f"\n=== Notification complete ===")
        if version:
            sync_outbox.summary(version)

    except Exception as e:
        error_msg = str(e)
        print(f"\nERROR: {error_msg}")
        slack_notify_error("notify_shift", error_msg)
        raise


def main():
    version = None
    for arg in sys.argv[1:]:
        if not arg.startswith("-"):
            version = arg
    run(schedule_version=version)


if __name__ == "__main__":
    main()
