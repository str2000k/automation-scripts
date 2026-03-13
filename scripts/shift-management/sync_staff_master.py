#!/usr/bin/env python3
"""Sync staff master: Google Spreadsheet → kintone app 213.

Reads 'スタッフマスタ' sheet, compares with kintone records by staff_id,
and performs add/update/deactivate operations.

Usage:
    python3 sync_staff_master.py              # dry-run (default)
    python3 sync_staff_master.py --no-dry-run # execute sync
"""
import base64
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta

from config import (
    KINTONE_DOMAIN, KINTONE_USERNAME, KINTONE_PASSWORD,
    KINTONE_STAFF_MASTER_APP_ID, SHIFT_SPREADSHEET_ID,
    SLACK_BOT_TOKEN, SLACK_SHIFT_CHANNEL, STAFF_ID_COLUMN,
)

GOOGLE_EMAIL = "satoru@chillaxy.jp"

# Spreadsheet field name -> kintone field code mapping
FIELD_MAP = {
    "staff_id": "staff_id",
    "氏名": "name",
    "雇用形態": "employment_type",
    "対応店舗": "store",
    "働き方": "shift_type",
    "役職": "position",
    "最大労働時間/週": "max_hours_per_week",
    "個人ルール": "personal_rule",
    "Slack ID": "slack_id",
    "LINE UID": "line_uid",
    "有効フラグ": "active",
}


# ---------------------------------------------------------------------------
# Google credential helpers
# ---------------------------------------------------------------------------

def _find_google_credential():
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


def kintone_get_all_records():
    """Fetch all records from kintone staff master app."""
    headers = _kintone_headers()
    all_records = []
    offset = 0
    while True:
        body = {
            "app": KINTONE_STAFF_MASTER_APP_ID,
            "query": f"order by staff_id asc limit 500 offset {offset}",
        }
        url = f"https://{KINTONE_DOMAIN}/k/v1/records.json"
        data = json.dumps(body).encode()
        req = urllib.request.Request(url, data=data, headers=headers, method="GET")
        with urllib.request.urlopen(req) as resp:
            result = json.loads(resp.read())
        records = result.get("records", [])
        all_records.extend(records)
        if len(records) < 500:
            break
        offset += 500
    return all_records


def kintone_add_records(records):
    headers = _kintone_headers()
    url = f"https://{KINTONE_DOMAIN}/k/v1/records.json"
    body = {"app": KINTONE_STAFF_MASTER_APP_ID, "records": records}
    data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read())


def kintone_update_records(records):
    headers = _kintone_headers()
    url = f"https://{KINTONE_DOMAIN}/k/v1/records.json"
    body = {"app": KINTONE_STAFF_MASTER_APP_ID, "records": records}
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


# ---------------------------------------------------------------------------
# Sync logic
# ---------------------------------------------------------------------------

def read_spreadsheet_staff():
    """Read staff master from spreadsheet. Returns list of dicts."""
    rows = sheets_read("スタッフマスタ!A1:L100")
    if not rows:
        raise RuntimeError("スタッフマスタが空です")

    headers = rows[0]
    staff = []
    for row in rows[1:]:
        if not row or not row[0]:
            continue
        while len(row) < len(headers):
            row.append("")
        d = {headers[i]: row[i] for i in range(len(headers))}
        staff.append(d)
    return staff


def build_kintone_record(sheet_row):
    """Convert a spreadsheet row dict to a kintone record body."""
    record = {}
    for sheet_field, kintone_field in FIELD_MAP.items():
        value = sheet_row.get(sheet_field, "")
        if kintone_field == "max_hours_per_week" and value:
            try:
                value = str(int(float(value)))
            except (ValueError, TypeError):
                value = ""
        record[kintone_field] = {"value": value}
    # Set synced_at
    record["synced_at"] = {"value": datetime.now(tz=__import__('datetime').timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
    return record


def diff_records(sheet_record, kintone_record):
    """Check if a kintone record needs updating. Returns list of changed fields."""
    changed = []
    for sheet_field, kintone_field in FIELD_MAP.items():
        sheet_val = str(sheet_record.get(sheet_field, "")).strip()
        kintone_val = str(kintone_record.get(kintone_field, {}).get("value", "")).strip()
        if kintone_field == "max_hours_per_week":
            # Normalize numeric comparison
            try:
                if float(sheet_val or "0") != float(kintone_val or "0"):
                    changed.append(kintone_field)
            except ValueError:
                if sheet_val != kintone_val:
                    changed.append(kintone_field)
        elif sheet_val != kintone_val:
            changed.append(kintone_field)
    return changed


def sync(dry_run=True):
    """Main sync logic."""
    print("=== Staff Master Sync (Spreadsheet → kintone) ===\n")

    # Validate app ID
    if not KINTONE_STAFF_MASTER_APP_ID:
        raise RuntimeError("KINTONE_STAFF_MASTER_APP_ID not set in config")

    print(f"  kintone App ID: {KINTONE_STAFF_MASTER_APP_ID}")
    print(f"  Spreadsheet: {SHIFT_SPREADSHEET_ID}")
    print(f"  dry-run: {dry_run}\n")

    # Read sources
    print("[1/3] Reading spreadsheet staff master...")
    sheet_staff = read_spreadsheet_staff()
    print(f"  Spreadsheet: {len(sheet_staff)} staff entries")

    print("[2/3] Reading kintone staff master...")
    kintone_records = kintone_get_all_records()
    kintone_by_id = {}
    for r in kintone_records:
        sid = r.get("staff_id", {}).get("value", "")
        if sid:
            kintone_by_id[sid] = r
    print(f"  kintone: {len(kintone_by_id)} existing records")

    # Diff
    print("[3/3] Computing diff...")
    to_add = []
    to_update = []
    unchanged = 0

    for row in sheet_staff:
        sid = row.get(STAFF_ID_COLUMN, "")
        if not sid:
            continue

        if sid in kintone_by_id:
            # Check for changes
            changed = diff_records(row, kintone_by_id[sid])
            if changed:
                record_id = kintone_by_id[sid]["$id"]["value"]
                kintone_rec = build_kintone_record(row)
                to_update.append({
                    "id": record_id,
                    "record": kintone_rec,
                    "_name": row.get("氏名", ""),
                    "_changed": changed,
                })
            else:
                unchanged += 1
        else:
            # New record
            kintone_rec = build_kintone_record(row)
            to_add.append({
                "record": kintone_rec,
                "_name": row.get("氏名", ""),
            })

    # Report
    print(f"\n  Results: add={len(to_add)}, update={len(to_update)}, unchanged={unchanged}")

    for item in to_add:
        print(f"    [ADD] {item['_name']}")
    for item in to_update:
        print(f"    [UPD] {item['_name']} (fields: {', '.join(item['_changed'])})")

    if dry_run:
        print("\n  [dry-run] No changes written.")
        return len(to_add), len(to_update)

    # Execute
    added = 0
    updated = 0

    if to_add:
        kintone_add_records([item["record"] for item in to_add])
        added = len(to_add)
        print(f"\n  Added {added} new records")

    if to_update:
        kintone_update_records([{"id": item["id"], "record": item["record"]} for item in to_update])
        updated = len(to_update)
        print(f"  Updated {updated} records")

    # Slack notification
    slack_notify(
        f"スタッフマスタ同期完了\n"
        f"新規追加: {added}件 / 更新: {updated}件 / 変更なし: {unchanged}件"
    )

    print(f"\n=== Sync complete (added: {added}, updated: {updated}) ===")
    return added, updated


def main():
    dry_run = "--no-dry-run" not in sys.argv
    if "--help" in sys.argv:
        print(__doc__)
        return
    sync(dry_run=dry_run)


if __name__ == "__main__":
    main()

# cron設定例（毎晩2時に実行）:
# 0 2 * * * cd /Users/s/Projects/automation-scripts/scripts/shift-management && python3 sync_staff_master.py --no-dry-run >> /tmp/sync_staff.log 2>&1
