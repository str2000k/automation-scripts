#!/usr/bin/env python3
"""STEP 10: Register confirmed shift data to kintone app 212.

Reads approved shift data from 'シフト出力' sheet and registers
one record per staff to kintone (確定シフト_chillaxy).
Existing records for the same period are updated to prevent duplicates.

Usage:
    python3 kintone_shift_register.py              # Auto-read from sheet
    python3 kintone_shift_register.py --dry-run    # Preview without registering
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
)

GOOGLE_EMAIL = "satoru@chillaxy.jp"


# ---------------------------------------------------------------------------
# Google credential helpers (same pattern as other modules)
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


def _get_token():
    cred_path = _find_google_credential()
    if not cred_path:
        raise RuntimeError("No Google OAuth credential found for satoru@chillaxy.jp")
    return _get_access_token(cred_path)


# ---------------------------------------------------------------------------
# Google Sheets API
# ---------------------------------------------------------------------------

def sheets_read(range_name):
    token = _get_token()
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


def kintone_get_records(query):
    """Fetch records from kintone confirmed shift app."""
    headers = _kintone_headers()
    encoded_query = urllib.parse.quote(query)
    url = (
        f"https://{KINTONE_DOMAIN}/k/v1/records.json"
        f"?app={KINTONE_SHIFT_CONFIRMED_APP_ID}"
        f"&query={encoded_query}&totalCount=true"
    )
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read())


def kintone_add_records(records):
    """Add multiple records to kintone."""
    headers = _kintone_headers()
    url = f"https://{KINTONE_DOMAIN}/k/v1/records.json"
    body = {
        "app": KINTONE_SHIFT_CONFIRMED_APP_ID,
        "records": records,
    }
    data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read())


def kintone_update_records(records):
    """Update multiple records in kintone."""
    headers = _kintone_headers()
    url = f"https://{KINTONE_DOMAIN}/k/v1/records.json"
    body = {
        "app": KINTONE_SHIFT_CONFIRMED_APP_ID,
        "records": records,
    }
    data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, headers=headers, method="PUT")
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read())


# ---------------------------------------------------------------------------
# Slack notification
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
# Read shift data from spreadsheet
# ---------------------------------------------------------------------------

def read_shift_output():
    """Read approved shift data from 'シフト出力' sheet.

    Returns (period_start, period_end, approver, approved_at, schedule_version,
             dates, staff_data).
    staff_data: list of {"staff_id", "name", "days": [{date, status}]}
    """
    rows = sheets_read("シフト出力!A1:R100")
    if len(rows) < 6:
        raise RuntimeError("シフト出力シートにデータがありません")

    # Row 1 (B1): period "2026-03-09 ~ 2026-03-22"
    period_str = rows[0][1] if len(rows[0]) > 1 else ""
    # Row 2 (B2): approver
    approver = rows[1][1] if len(rows) > 1 and len(rows[1]) > 1 else ""
    # Row 3 (B3): approved_at
    approved_at = rows[2][1] if len(rows) > 2 and len(rows[2]) > 1 else ""
    # Row 4 (B4): schedule_version
    schedule_version = rows[3][1] if len(rows) > 3 and len(rows[3]) > 1 else ""

    if not approver:
        raise RuntimeError("シフトが未承認です。先に承認してください。")

    # Parse period_start / period_end from period_str
    period_parts = period_str.split("~")
    period_start = period_parts[0].strip() if len(period_parts) >= 1 else ""
    period_end = period_parts[1].strip() if len(period_parts) >= 2 else ""

    # Row 5 (index 4): date headers (A5=staff_id, B5=スタッフ名, C5+=dates)
    header_row = rows[4]
    dates = []
    for cell in header_row[2:]:  # skip staff_id and name columns
        date_part = cell.split("\n")[0].strip() if cell else ""
        if date_part and len(date_part) == 10:
            dates.append(date_part)

    # Row 6+ (index 5+): staff data with per-day breakdown
    staff_data = []
    for row in rows[5:]:
        if not row or not row[0]:
            continue
        staff_id = row[0]
        name = row[1] if len(row) > 1 else ""
        days = []
        for i, date in enumerate(dates):
            col_idx = i + 2
            status = row[col_idx] if col_idx < len(row) else "休み"
            days.append({"date": date, "status": status})

        staff_data.append({
            "staff_id": staff_id,
            "name": name,
            "days": days,
        })

    print(f"  Period: {period_start} ~ {period_end}")
    print(f"  Approver: {approver}")
    print(f"  Staff: {len(staff_data)} members, {len(dates)} days")

    return period_start, period_end, approver, approved_at, schedule_version, dates, staff_data


# ---------------------------------------------------------------------------
# kintone registration logic
# ---------------------------------------------------------------------------

def fetch_existing_records(period_start, period_end):
    """Fetch existing records for the same period. Returns {(staff_id, shift_date): record_id}."""
    query = (
        f'period_start = "{period_start}" and period_end = "{period_end}" '
        f'order by staff_id asc, shift_date asc'
    )
    result = kintone_get_records(query)
    records = result.get("records", [])

    existing = {}
    for r in records:
        sid = r.get("staff_id", {}).get("value", "")
        shift_date = r.get("shift_date", {}).get("value", "")
        record_id = r["$id"]["value"]
        if sid and shift_date:
            existing[(sid, shift_date)] = record_id

    print(f"  Existing records for this period: {len(existing)}")
    return existing


def build_kintone_record_per_day(staff_id, staff_name, day_info, period_start,
                                  period_end, approver, approved_at, schedule_version=""):
    """Build a kintone record body for one staff x one day."""
    record = {
        "staff_id": {"value": staff_id},
        "staff_name": {"value": staff_name},
        "shift_date": {"value": day_info["date"]},
        "shift_status": {"value": day_info["status"]},
        "period_start": {"value": period_start},
        "period_end": {"value": period_end},
        "confirmed_by": {"value": approver},
        "confirmed_at": {"value": approved_at},
    }
    if schedule_version:
        record["schedule_version"] = {"value": schedule_version}
    return record


def register_to_kintone(staff_data, period_start, period_end, approver, approved_at,
                        schedule_version="", dry_run=False):
    """Register or update confirmed shift records in kintone (1 record per staff per day)."""
    existing = fetch_existing_records(period_start, period_end)

    to_add = []
    to_update = []

    for staff in staff_data:
        sid = staff.get("staff_id", "")
        name = staff.get("name", "")
        for day_info in staff.get("days", []):
            record = build_kintone_record_per_day(
                sid, name, day_info, period_start, period_end,
                approver, approved_at, schedule_version,
            )
            key = (sid, day_info["date"])
            record_id = existing.get(key)
            if record_id:
                to_update.append({"id": record_id, "record": record})
            else:
                to_add.append(record)

    total_work = sum(
        1 for s in staff_data for d in s.get("days", []) if d["status"] == "出勤"
    )
    total_off = sum(
        1 for s in staff_data for d in s.get("days", []) if d["status"] != "出勤"
    )

    if dry_run:
        print(f"  [dry-run] Would add {len(to_add)} / update {len(to_update)} records")
        print(f"  Total: {total_work} work-day records, {total_off} off-day records")
        return len(to_add), len(to_update)

    added = 0
    updated = 0

    # kintone allows max 100 records per request
    for i in range(0, len(to_add), 100):
        batch = to_add[i:i + 100]
        kintone_add_records(batch)
        added += len(batch)

    for i in range(0, len(to_update), 100):
        batch = to_update[i:i + 100]
        kintone_update_records(batch)
        updated += len(batch)

    if added:
        print(f"  Added {added} new records")
    if updated:
        print(f"  Updated {updated} existing records")

    return added, updated


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run(dry_run=False, schedule_version=None):
    import sync_outbox

    print("=== kintone Shift Register (STEP 10) ===\n")

    try:
        # (1) Read shift output
        print("[1/2] Reading shift data from spreadsheet...")
        period_start, period_end, approver, approved_at, sv, dates, staff_data = read_shift_output()
        version = schedule_version or sv
        period_str = f"{period_start} ~ {period_end}"
        print(f"  Schedule version: {version}")

        # Check outbox
        if version and not dry_run and not sync_outbox.should_run(version, "kintone"):
            print("  kintone already registered for this version, skipping.")
            return

        # (2) Register to kintone (1 record per staff per day)
        print("[2/2] Registering to kintone...")
        added, updated = register_to_kintone(
            staff_data, period_start, period_end, approver, approved_at,
            schedule_version=version, dry_run=dry_run,
        )

        print(f"\n=== Registration complete (added: {added}, updated: {updated}) ===")

        if not dry_run:
            if version:
                sync_outbox.update_status(version, "kintone", "sent")
            slack_notify(
                f"\u2705 kintone確定シフト登録完了\n"
                f"対象期間: {period_str}\n"
                f"新規登録: {added}件 / 更新: {updated}件\n"
                f"承認者: {approver}\n"
                f"バージョン: {version}"
            )

    except Exception as e:
        error_msg = str(e)
        print(f"\nERROR: {error_msg}")
        if version and not dry_run:
            sync_outbox.update_status(version, "kintone", "failed", error_msg)
        slack_notify_error("kintone_shift_register", error_msg)
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
