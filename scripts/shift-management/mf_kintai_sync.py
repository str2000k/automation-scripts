#!/usr/bin/env python3
"""STEP E: Sync confirmed shift to MF Cloud Attendance (MFクラウド勤怠).

Reads confirmed shift data from kintone app 212, then registers
shift schedules to MF Cloud Attendance REST API v1.

Usage:
    python3 mf_kintai_sync.py --version 2026-04-01_v1          # dry-run (default)
    python3 mf_kintai_sync.py --version 2026-04-01_v1 --no-dry-run  # live
    python3 mf_kintai_sync.py --status                          # check outbox status
"""
import base64
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime

from config import (
    KINTONE_DOMAIN, KINTONE_USERNAME, KINTONE_PASSWORD,
    KINTONE_SHIFT_CONFIRMED_APP_ID,
    SLACK_BOT_TOKEN, SLACK_SHIFT_CHANNEL,
    MF_CLIENT_ID, MF_CLIENT_SECRET,
)

import sync_outbox

# MF Cloud Attendance API
MF_API_BASE = "https://attendance.moneyforward.com/api/v1"
MF_ACCESS_TOKEN_KEY = "MF_ACCESS_TOKEN"


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------

def _get_mf_token():
    """Get MF Cloud Attendance access token from config."""
    from config import MF_ACCESS_TOKEN
    if not MF_ACCESS_TOKEN:
        raise RuntimeError(
            f"{MF_ACCESS_TOKEN_KEY} not set in .env. "
            "Set the OAuth2 access token for MF Cloud Attendance."
        )
    return MF_ACCESS_TOKEN


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


def fetch_shift_from_kintone(schedule_version):
    """Fetch confirmed shift records from kintone ID=212.

    New schema: 1 record per staff per day.
    Groups by staff_id and collects working days (shift_status=出勤).

    Returns list of dicts: {staff_id, staff_name, working_days, period_start, period_end}
    """
    headers = _kintone_headers()
    query = f'schedule_version = "{schedule_version}" order by staff_id asc, shift_date asc'
    body = {
        "app": KINTONE_SHIFT_CONFIRMED_APP_ID,
        "query": query,
        "totalCount": True,
    }
    url = f"https://{KINTONE_DOMAIN}/k/v1/records.json"
    data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, headers=headers, method="GET")
    with urllib.request.urlopen(req) as resp:
        result_data = json.loads(resp.read())

    records = result_data.get("records", [])
    if not records:
        raise RuntimeError(
            f"No confirmed shift records found for version: {schedule_version}"
        )

    # Group by staff_id
    staff_map = {}
    for r in records:
        staff_id = r.get("staff_id", {}).get("value", "")
        if not staff_id:
            continue
        if staff_id not in staff_map:
            staff_map[staff_id] = {
                "staff_id": staff_id,
                "staff_name": r.get("staff_name", {}).get("value", ""),
                "working_days": [],
                "period_start": r.get("period_start", {}).get("value", ""),
                "period_end": r.get("period_end", {}).get("value", ""),
            }
        shift_status = r.get("shift_status", {}).get("value", "")
        shift_date = r.get("shift_date", {}).get("value", "")
        if shift_status == "出勤" and shift_date:
            staff_map[staff_id]["working_days"].append(shift_date)

    result = list(staff_map.values())
    print(f"  Fetched {len(records)} records -> {len(result)} staff from kintone")
    return result


# ---------------------------------------------------------------------------
# MF Cloud Attendance API
# ---------------------------------------------------------------------------

def _mf_api_request(method, path, body=None):
    """Make an API request to MF Cloud Attendance."""
    token = _get_mf_token()
    url = f"{MF_API_BASE}{path}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }

    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)

    try:
        with urllib.request.urlopen(req) as resp:
            resp_body = resp.read()
            if resp_body:
                return json.loads(resp_body)
            return {}
    except urllib.error.HTTPError as e:
        error_body = e.read().decode() if e.fp else ""
        raise RuntimeError(
            f"MF API error {e.code}: {error_body} (path={path})"
        ) from e


def register_shift_to_mf(staff_id, staff_name, working_days, dry_run=False):
    """Register shift schedule for one staff member to MF Cloud Attendance.

    Args:
        staff_id: Staff ID (S001 format)
        staff_name: Staff name (display only)
        working_days: List of date strings (YYYY-MM-DD)
        dry_run: If True, only print what would be done

    Returns:
        Number of days registered
    """
    if dry_run:
        print(f"  [dry-run] {staff_id} {staff_name}: "
              f"{len(working_days)} work days")
        for day in working_days:
            print(f"    - {day}")
        return len(working_days)

    # Register each work day as a shift assignment
    registered = 0
    for day in working_days:
        body = {
            "employee_code": staff_id,
            "date": day,
            "shift_type": "normal",
        }
        try:
            _mf_api_request("PUT", "/shift_assignments", body=body)
            registered += 1
        except RuntimeError as e:
            print(f"  [WARN] {staff_id} {day}: {e}")
            raise

    print(f"  {staff_id} {staff_name}: registered {registered} days")
    return registered


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
# Main logic
# ---------------------------------------------------------------------------

def run(dry_run=True, schedule_version=None):
    """Run MF Cloud Attendance sync.

    Called from run_post_approval.py or CLI.
    """
    print("=== MF Cloud Attendance Sync (STEP E) ===\n")

    version = schedule_version
    if not version:
        version = sync_outbox.get_latest_version()
    if not version:
        print("No schedule_version specified or found in outbox.")
        return

    print(f"  Schedule version: {version}")
    print(f"  Mode: {'dry-run' if dry_run else 'LIVE'}")

    try:
        # (1) Check outbox
        if not dry_run and not sync_outbox.should_run(version, "mf_kintai"):
            print("  MF Kintai already synced for this version, skipping.")
            return

        # (2) Fetch from kintone
        print("\n[1/2] Fetching confirmed shift from kintone...")
        records = fetch_shift_from_kintone(version)

        # (3) Register to MF Cloud Attendance
        print("\n[2/2] Registering to MF Cloud Attendance...")
        total_days = 0
        errors = []

        for record in records:
            try:
                days = register_shift_to_mf(
                    record["staff_id"],
                    record["staff_name"],
                    record["working_days"],
                    dry_run=dry_run,
                )
                total_days += days
            except Exception as e:
                error_msg = f"{record['staff_id']} {record['staff_name']}: {e}"
                errors.append(error_msg)
                print(f"  [ERROR] {error_msg}")

        print(f"\n=== Sync {'preview' if dry_run else 'complete'} "
              f"(staff: {len(records)}, total days: {total_days}) ===")

        if not dry_run:
            if errors:
                error_detail = "\n".join(errors)
                sync_outbox.update_status(
                    version, "mf_kintai", "partial_failed",
                    f"{len(errors)} staff failed"
                )
                slack_notify_error(
                    "mf_kintai_sync",
                    f"{len(errors)}件のエラー:\n{error_detail}"
                )
            else:
                sync_outbox.update_status(version, "mf_kintai", "sent")
                slack_notify(
                    f"MFクラウド勤怠同期完了\n"
                    f"バージョン: {version}\n"
                    f"スタッフ: {len(records)}名 / 合計出勤日: {total_days}日"
                )

    except Exception as e:
        error_msg = str(e)
        print(f"\nERROR: {error_msg}")
        if not dry_run:
            try:
                sync_outbox.update_status(version, "mf_kintai", "failed", error_msg)
            except ValueError:
                pass
            slack_notify_error("mf_kintai_sync", error_msg)
        raise


def main():
    if "--status" in sys.argv:
        version = None
        for arg in sys.argv[1:]:
            if not arg.startswith("-"):
                version = arg
        sync_outbox.summary(version)
        return

    if "--help" in sys.argv:
        print(__doc__)
        return

    dry_run = "--no-dry-run" not in sys.argv

    version = None
    for i, arg in enumerate(sys.argv[1:], 1):
        if arg == "--version" and i < len(sys.argv) - 1:
            version = sys.argv[i + 1]

    run(dry_run=dry_run, schedule_version=version)


if __name__ == "__main__":
    main()
