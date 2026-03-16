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

def read_staff_master():
    """Read staff master keyed by name. Returns {name: {staff_id, ...}}."""
    rows = sheets_read("スタッフマスタ!A1:P100")
    if not rows:
        return {}
    header = rows[0]
    name_map = {}
    for row in rows[1:]:
        if not row or not row[0]:
            continue
        row += [""] * (len(header) - len(row))
        d = {header[i]: row[i] for i in range(len(header))}
        name = d.get("氏名", "")
        if name and d.get("有効フラグ", "") != "無効":
            name_map[name] = d
    return name_map


def read_shift_output():
    """Read shift data from 'シフト出力' sheet (store-based format).

    Layout:
      Row 1: metadata (シフト期間 + year/month dropdowns)
      Row 2: store group headers
      Row 3: sub-headers (早番, 遅番, ..., staff_names)
      Row 4: label row (確定, 変更, blank, 休日) + rest day counts
      Row 5+: data rows

    Returns (period_start, period_end, approver, approved_at, schedule_version,
             dates, staff_data).
    staff_data: list of {"staff_id", "name", "days": [{date, status, time, store}]}
    """
    rows = sheets_read("シフト出力!A1:BZ200")
    if len(rows) < 5:
        raise RuntimeError("シフト出力シートにデータがありません")

    # Row 1: metadata
    period_str = rows[0][1] if len(rows[0]) > 1 else ""
    approver = ""
    approved_at = ""
    schedule_version = ""

    period_parts = period_str.split("~")
    period_start = period_parts[0].strip() if len(period_parts) >= 1 else ""
    period_end = period_parts[1].strip() if len(period_parts) >= 2 else ""

    # Row 2 (index 1): store headers
    store_header = rows[1] if len(rows) > 1 else []
    # Row 3 (index 2): sub-headers
    sub_header = rows[2] if len(rows) > 2 else []
    # Row 4 (index 3): label row (確定/変更/blank/休日) - skip

    # Identify staff columns (individual time section)
    system_labels = {"確定", "変更", "", "早番", "遅番", "休日"}
    staff_columns = {}  # col_idx -> staff_name
    for i, label in enumerate(sub_header):
        if label and label not in system_labels:
            staff_columns[i] = label

    # Identify store assignment columns for lookup
    store_early_cols = {}  # col_idx -> store_name (早番)
    store_late_cols = {}   # col_idx -> store_name (遅番)
    for i, label in enumerate(sub_header):
        if label == "早番" and i > 0:
            # Find store name from store_header
            for j in range(i, -1, -1):
                if j < len(store_header) and store_header[j]:
                    store_early_cols[i] = store_header[j]
                    break
        elif label == "遅番" and i > 0:
            for j in range(i, -1, -1):
                if j < len(store_header) and store_header[j]:
                    store_late_cols[i] = store_header[j]
                    break

    # Read staff master for staff_id lookup
    name_master = read_staff_master()

    # Parse year from period
    year = period_start.split("-")[0] if period_start else str(datetime.now().year)

    # Extract dates and build per-staff data (row 5+ = index 4+)
    data_rows = rows[4:]
    dates = []
    for row in data_rows:
        if len(row) > 2 and row[2]:
            dl = str(row[2]).strip()
            if "/" in dl:
                parts = dl.split("/")
                try:
                    dates.append(f"{year}-{int(parts[0]):02d}-{int(parts[1]):02d}")
                except (ValueError, IndexError):
                    dates.append(dl)

    # Build per-staff data
    staff_data = []
    for col_idx, staff_name in staff_columns.items():
        master = name_master.get(staff_name, {})
        staff_id = master.get("staff_id", "")
        days = []

        for row_idx, row in enumerate(data_rows):
            if row_idx >= len(dates):
                break
            date = dates[row_idx]
            time_val = row[col_idx] if col_idx < len(row) else ""

            # Find which store this person is assigned to on this day
            store = ""
            for ec, sn in store_early_cols.items():
                if ec < len(row) and row[ec] == staff_name:
                    store = sn
                    break
            if not store:
                for lc, sn in store_late_cols.items():
                    if lc < len(row) and row[lc] == staff_name:
                        store = sn
                        break

            status = "出勤" if time_val else "休み"
            days.append({
                "date": date,
                "status": status,
                "time": time_val,
                "store": store,
            })

        staff_data.append({
            "staff_id": staff_id,
            "name": staff_name,
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
    # Store assignment and time range from new format
    store = day_info.get("store", "")
    if store:
        record["store"] = {"value": store}
    time_range = day_info.get("time", "")
    if time_range and "-" in time_range:
        parts = time_range.split("-")
        record["start_time"] = {"value": parts[0].strip()}
        record["end_time"] = {"value": parts[1].strip()}
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
