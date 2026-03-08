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

    Returns (period_str, approver, approved_at, dates, staff_data).
    staff_data: list of {"name", "working_days": [dates], "off_days": [dates]}
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

    # Row 5 (index 4): date headers (A5=staff_id, B5=スタッフ名, C5+=dates)
    header_row = rows[4]
    dates = []
    for cell in header_row[2:]:  # skip staff_id and name columns
        date_part = cell.split("\n")[0].strip() if cell else ""
        if date_part and len(date_part) == 10:
            dates.append(date_part)

    # Row 6+ (index 5+): staff data
    staff_data = []
    for row in rows[5:]:
        if not row or not row[0]:
            continue
        staff_id = row[0]
        name = row[1] if len(row) > 1 else ""
        working_days = []
        off_days = []
        for i, date in enumerate(dates):
            col_idx = i + 2  # shifted by staff_id + name columns
            status = row[col_idx] if col_idx < len(row) else "休み"
            if status == "出勤":
                working_days.append(date)
            else:
                off_days.append(date)

        staff_data.append({
            "staff_id": staff_id,
            "name": name,
            "working_days": working_days,
            "off_days": off_days,
        })

    print(f"  Period: {period_str}")
    print(f"  Approver: {approver}")
    print(f"  Staff: {len(staff_data)} members")

    return period_str, approver, approved_at, schedule_version, dates, staff_data


# ---------------------------------------------------------------------------
# kintone registration logic
# ---------------------------------------------------------------------------

def fetch_existing_records(period_str):
    """Fetch existing records for the same period. Returns {staff_id: record_id}."""
    query = f'shift_period = "{period_str}" order by staff_name asc'
    result = kintone_get_records(query)
    records = result.get("records", [])

    existing = {}
    for r in records:
        sid = r.get("staff_id", {}).get("value", "")
        if not sid:
            sid = r["staff_name"]["value"]  # fallback for legacy records
        record_id = r["$id"]["value"]
        existing[sid] = record_id

    print(f"  Existing records for this period: {len(existing)}")
    return existing


def build_kintone_record(staff, period_str, approver, approved_at, schedule_version=""):
    """Build a kintone record body for one staff member."""
    record = {
        "shift_period": {"value": period_str},
        "staff_id": {"value": staff.get("staff_id", "")},
        "staff_name": {"value": staff["name"]},
        "working_days": {"value": ", ".join(staff["working_days"])},
        "off_days": {"value": ", ".join(staff["off_days"])},
        "confirmed_by": {"value": approver},
        "confirmed_at": {"value": approved_at},
    }
    if schedule_version:
        record["schedule_version"] = {"value": schedule_version}
    return record


def register_to_kintone(staff_data, period_str, approver, approved_at,
                        schedule_version="", dry_run=False):
    """Register or update confirmed shift records in kintone."""
    existing = fetch_existing_records(period_str)

    to_add = []
    to_update = []

    for staff in staff_data:
        record = build_kintone_record(staff, period_str, approver, approved_at,
                                      schedule_version)
        sid = staff.get("staff_id", staff["name"])
        record_id = existing.get(sid)

        if record_id:
            to_update.append({"id": record_id, "record": record})
        else:
            to_add.append(record)

    if dry_run:
        print(f"  [dry-run] Would add {len(to_add)} / update {len(to_update)} records")
        for staff in staff_data:
            sid = staff.get("staff_id", staff["name"])
            action = "update" if sid in existing else "add"
            print(f"    [{action}] {staff.get('staff_id', '')} {staff['name']}: "
                  f"出勤{len(staff['working_days'])}日, 休{len(staff['off_days'])}日")
        return len(to_add), len(to_update)

    added = 0
    updated = 0

    if to_add:
        kintone_add_records(to_add)
        added = len(to_add)
        print(f"  Added {added} new records")

    if to_update:
        kintone_update_records(to_update)
        updated = len(to_update)
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
        period_str, approver, approved_at, sv, dates, staff_data = read_shift_output()
        version = schedule_version or sv
        print(f"  Schedule version: {version}")

        # Check outbox
        if version and not dry_run and not sync_outbox.should_run(version, "kintone"):
            print("  kintone already registered for this version, skipping.")
            return

        # (2) Register to kintone
        print("[2/2] Registering to kintone...")
        added, updated = register_to_kintone(
            staff_data, period_str, approver, approved_at,
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
