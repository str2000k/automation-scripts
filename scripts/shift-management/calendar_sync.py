#!/usr/bin/env python3
"""STEP 9: Sync shift schedule to Google Calendar.

Reads the 'シフト出力' sheet from the shift management spreadsheet,
then creates all-day calendar events for each staff member's work days.

- Title: [氏名]出勤
- Description: 役割・スキル from スタッフマスタ F column
- Existing events in the period are deleted before re-creating (duplicate prevention)
- Errors are notified to Slack admin channel

Usage:
    python3 calendar_sync.py              # Sync current shift period
    python3 calendar_sync.py --dry-run    # Preview without creating events
"""
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta

from config import (
    SHIFT_SPREADSHEET_ID,
    SLACK_BOT_TOKEN,
    SLACK_SHIFT_CHANNEL,
)

GOOGLE_EMAIL = "satoru@chillaxy.jp"
CALENDAR_ID = GOOGLE_EMAIL  # Use primary calendar
EVENT_TAG = "[shift-sync]"  # Tag in description to identify managed events


# ---------------------------------------------------------------------------
# Google credential helpers (same pattern as generate_shift.py)
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
        import datetime as dt_mod
        new_expiry = (datetime.now(dt_mod.timezone.utc) + timedelta(seconds=expires_in)).isoformat()
        cred["expiry"] = new_expiry
        cred["token_expiry"] = new_expiry
        with open(cred_path, "w") as f:
            json.dump(cred, f, indent=2)

        return new_token

    if token:
        return token

    raise RuntimeError(f"Cannot get access token from {cred_path}")


def get_token():
    cred_path = _find_google_credential()
    if not cred_path:
        raise RuntimeError("No Google OAuth credential found for satoru@chillaxy.jp")
    return _get_access_token(cred_path)


# ---------------------------------------------------------------------------
# Google Sheets API
# ---------------------------------------------------------------------------

def sheets_read(range_name):
    token = get_token()
    encoded_range = urllib.parse.quote(range_name)
    url = (
        f"https://sheets.googleapis.com/v4/spreadsheets/{SHIFT_SPREADSHEET_ID}"
        f"/values/{encoded_range}"
    )
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req) as resp:
        data = json.loads(resp.read())
    return data.get("values", [])


# ---------------------------------------------------------------------------
# Google Calendar API
# ---------------------------------------------------------------------------

def calendar_list_events(time_min, time_max):
    """List events in the given time range that were created by this script."""
    token = get_token()
    params = urllib.parse.urlencode({
        "timeMin": time_min + "T00:00:00+09:00",
        "timeMax": time_max + "T23:59:59+09:00",
        "maxResults": 500,
        "singleEvents": "true",
    })
    encoded_cal = urllib.parse.quote(CALENDAR_ID)
    url = f"https://www.googleapis.com/calendar/v3/calendars/{encoded_cal}/events?{params}"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req) as resp:
        data = json.loads(resp.read())
    return data.get("items", [])


def calendar_delete_event(event_id):
    token = get_token()
    encoded_cal = urllib.parse.quote(CALENDAR_ID)
    url = f"https://www.googleapis.com/calendar/v3/calendars/{encoded_cal}/events/{event_id}"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"}, method="DELETE")
    with urllib.request.urlopen(req) as resp:
        resp.read()


def calendar_create_event(date_str, summary, description):
    """Create an all-day event."""
    token = get_token()
    encoded_cal = urllib.parse.quote(CALENDAR_ID)
    url = f"https://www.googleapis.com/calendar/v3/calendars/{encoded_cal}/events"

    # All-day event: end date is exclusive (next day)
    start_date = datetime.strptime(date_str, "%Y-%m-%d")
    end_date = start_date + timedelta(days=1)

    body = {
        "summary": summary,
        "description": f"{description}\n{EVENT_TAG}",
        "start": {"date": date_str},
        "end": {"date": end_date.strftime("%Y-%m-%d")},
        "transparency": "transparent",  # Don't block time
    }

    data = json.dumps(body).encode()
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read())


# ---------------------------------------------------------------------------
# Slack notification
# ---------------------------------------------------------------------------

def slack_notify_error(location, error):
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    text = f"【エラー通知】発生箇所:{location} エラー:{error} 日時:{now}"
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


def slack_notify_success(period_start, period_end, created_count, deleted_count):
    if not SLACK_BOT_TOKEN:
        return
    text = (
        f"*カレンダー同期完了*\n"
        f"対象期間: {period_start} ~ {period_end}\n"
        f"削除イベント: {deleted_count}件 / 登録イベント: {created_count}件\n"
        f"Googleカレンダーを確認してください。"
    )
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
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Core logic
# ---------------------------------------------------------------------------

def read_shift_output():
    """Read shift output sheet. Returns (period_start, period_end, dates, staff_schedule).

    staff_schedule: list of {"name": str, "dates": {date_str: "出勤"|"休み"|...}}
    """
    rows = sheets_read("シフト出力!A1:R100")
    if not rows:
        raise RuntimeError("シフト出力シートが空です")

    # Row 1: period
    period_str = rows[0][1] if len(rows[0]) > 1 else ""
    parts = period_str.split("~")
    if len(parts) != 2:
        raise RuntimeError(f"シフト期間の形式が不正です: {period_str}")
    period_start = parts[0].strip()
    period_end = parts[1].strip()

    # Row 4: schedule_version
    schedule_version = rows[3][1] if len(rows) > 3 and len(rows[3]) > 1 else ""

    # Row 6 (index 5): date headers (A6=staff_id, B6=スタッフ名, C6+=dates)
    header_row = rows[5]
    dates = []
    for cell in header_row[2:]:  # skip staff_id and name columns
        date_part = cell.split("\n")[0].strip() if cell else ""
        if date_part and len(date_part) == 10:
            dates.append(date_part)

    # Row 7+ (index 6+): staff data
    staff_schedule = []
    for row in rows[6:]:
        if not row or not row[0]:
            continue
        staff_id = row[0]
        name = row[1] if len(row) > 1 else ""
        if name in ("", "出勤日数", "合計勤務時間"):
            continue
        schedule = {}
        for i, date in enumerate(dates):
            col_idx = i + 2  # shifted by staff_id + name columns
            status = row[col_idx] if col_idx < len(row) else "休み"
            schedule[date] = status
        staff_schedule.append({"staff_id": staff_id, "name": name, "dates": schedule})

    print(f"  Period: {period_start} ~ {period_end}")
    print(f"  Dates: {len(dates)} days")
    print(f"  Staff: {len(staff_schedule)} members")

    return period_start, period_end, schedule_version, dates, staff_schedule


def read_staff_roles():
    """Read staff master to get role info. Returns {staff_id: role}."""
    rows = sheets_read("スタッフマスタ!A1:Q100")
    if not rows:
        return {}

    header = rows[0]
    roles = {}
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
        role = d.get("役職", "")
        if staff_id:
            roles[staff_id] = role

    print(f"  Staff roles loaded: {len(roles)} entries")
    return roles


def delete_existing_events(period_start, period_end):
    """Delete events created by this script in the given period."""
    events = calendar_list_events(period_start, period_end)

    # Filter to events created by this script (contain EVENT_TAG in description)
    managed = [e for e in events if EVENT_TAG in (e.get("description") or "")]

    if not managed:
        print("  No existing sync events to delete")
        return 0

    deleted = 0
    for event in managed:
        try:
            calendar_delete_event(event["id"])
            deleted += 1
        except urllib.error.HTTPError as e:
            if e.code == 410:  # Already deleted
                continue
            raise

    print(f"  Deleted {deleted} existing sync events")
    return deleted


def create_shift_events(staff_schedule, roles, dry_run=False):
    """Create all-day calendar events for staff work days."""
    created = 0
    for staff in staff_schedule:
        name = staff["name"]
        role = roles.get(staff.get("staff_id", ""), "")
        for date_str, status in staff["dates"].items():
            if status != "出勤":
                continue

            summary = f"{name}出勤"
            description = role if role else ""

            if dry_run:
                print(f"  [dry-run] {date_str}: {summary} ({description})")
            else:
                calendar_create_event(date_str, summary, description)
            created += 1

    action = "Would create" if dry_run else "Created"
    print(f"  {action} {created} events")
    return created


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run(dry_run=False, schedule_version=None):
    import sync_outbox

    print("=== Calendar Sync (STEP 9) ===\n")

    try:
        # (1) Read shift output
        print("[1/4] Reading shift output...")
        period_start, period_end, sv, dates, staff_schedule = read_shift_output()
        version = schedule_version or sv
        print(f"  Schedule version: {version}")

        # Check outbox
        if version and not dry_run and not sync_outbox.should_run(version, "calendar"):
            print("  Calendar already synced for this version, skipping.")
            return

        # (2) Read staff roles
        print("[2/4] Reading staff roles...")
        roles = read_staff_roles()

        # (3) Delete existing events in the period
        print("[3/4] Deleting existing sync events...")
        if dry_run:
            print("  [dry-run] Skipping deletion")
            deleted = 0
        else:
            deleted = delete_existing_events(period_start, period_end)

        # (4) Create new events
        print("[4/4] Creating calendar events...")
        created = create_shift_events(staff_schedule, roles, dry_run=dry_run)

        print(f"\n=== Calendar sync complete (deleted: {deleted}, created: {created}) ===")

        if not dry_run:
            if version:
                sync_outbox.update_status(version, "calendar", "sent")
            slack_notify_success(period_start, period_end, created, deleted)

    except Exception as e:
        error_msg = str(e)
        print(f"\nERROR: {error_msg}")
        if version and not dry_run:
            sync_outbox.update_status(version, "calendar", "failed", error_msg)
        slack_notify_error("calendar_sync", error_msg)
        raise


def main():
    dry_run = "--dry-run" in sys.argv
    if "--help" in sys.argv:
        print(__doc__)
        return
    version = None
    for arg in sys.argv[1:]:
        if not arg.startswith("-"):
            version = arg
    run(dry_run=dry_run, schedule_version=version)


if __name__ == "__main__":
    main()
