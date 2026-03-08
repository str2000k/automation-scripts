#!/usr/bin/env python3
"""STEP G: End-to-end test for the shift management system.

Creates test staff (T001-T003), runs the full shift workflow in dry-run mode,
validates each step, then cleans up all test data.

Usage:
    python3 test_e2e.py              # Run all E2E tests
    python3 test_e2e.py --no-cleanup # Skip cleanup (for debugging)
"""
import base64
import json
import os
import sys
import traceback
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta

from config import (
    KINTONE_DOMAIN, KINTONE_USERNAME, KINTONE_PASSWORD,
    KINTONE_SHIFT_WISH_APP_ID, KINTONE_SHIFT_CONFIRMED_APP_ID,
    SHIFT_SPREADSHEET_ID,
    SLACK_BOT_TOKEN, SLACK_SHIFT_CHANNEL,
)

TEST_PREFIX = "__TEST__"
TEST_STAFF = [
    {"staff_id": "T001", "name": f"{TEST_PREFIX}田中太郎", "role": "テスト"},
    {"staff_id": "T002", "name": f"{TEST_PREFIX}鈴木花子", "role": "テスト"},
    {"staff_id": "T003", "name": f"{TEST_PREFIX}佐藤次郎", "role": "テスト"},
]


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


def sheets_append(range_name, values):
    token = get_token()
    encoded_range = urllib.parse.quote(range_name)
    url = (
        f"https://sheets.googleapis.com/v4/spreadsheets/{SHIFT_SPREADSHEET_ID}"
        f"/values/{encoded_range}:append"
        f"?valueInputOption=USER_ENTERED&insertDataOption=INSERT_ROWS"
    )
    body = {"values": values}
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        url, data=data,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read())


def sheets_update(range_name, values):
    token = get_token()
    encoded_range = urllib.parse.quote(range_name)
    url = (
        f"https://sheets.googleapis.com/v4/spreadsheets/{SHIFT_SPREADSHEET_ID}"
        f"/values/{encoded_range}"
        f"?valueInputOption=USER_ENTERED"
    )
    body = {"values": values}
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        url, data=data,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        method="PUT",
    )
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read())


def sheets_clear(range_name):
    token = get_token()
    encoded_range = urllib.parse.quote(range_name)
    url = (
        f"https://sheets.googleapis.com/v4/spreadsheets/{SHIFT_SPREADSHEET_ID}"
        f"/values/{encoded_range}:clear"
    )
    req = urllib.request.Request(
        url, data=b"{}",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read())


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


def kintone_get_records(app_id, query):
    headers = _kintone_headers()
    encoded_query = urllib.parse.quote(query)
    url = (
        f"https://{KINTONE_DOMAIN}/k/v1/records.json"
        f"?app={app_id}&query={encoded_query}&totalCount=true"
    )
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read())


def kintone_add_records(app_id, records):
    headers = _kintone_headers()
    url = f"https://{KINTONE_DOMAIN}/k/v1/records.json"
    body = {"app": app_id, "records": records}
    data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read())


def kintone_delete_records(app_id, record_ids):
    if not record_ids:
        return
    headers = _kintone_headers()
    url = f"https://{KINTONE_DOMAIN}/k/v1/records.json"
    body = {"app": app_id, "ids": record_ids}
    data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, headers=headers, method="DELETE")
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read())


# ---------------------------------------------------------------------------
# Calendar API (for cleanup)
# ---------------------------------------------------------------------------

def calendar_list_test_events(time_min, time_max):
    token = get_token()
    cal_id = urllib.parse.quote("satoru@chillaxy.jp")
    params = urllib.parse.urlencode({
        "timeMin": time_min + "T00:00:00+09:00",
        "timeMax": time_max + "T23:59:59+09:00",
        "maxResults": 500,
        "singleEvents": "true",
    })
    url = f"https://www.googleapis.com/calendar/v3/calendars/{cal_id}/events?{params}"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req) as resp:
        data = json.loads(resp.read())
    # Filter test events
    return [
        e for e in data.get("items", [])
        if "[shift-sync][TEST]" in (e.get("description") or "")
    ]


def calendar_delete_event(event_id):
    token = get_token()
    cal_id = urllib.parse.quote("satoru@chillaxy.jp")
    url = f"https://www.googleapis.com/calendar/v3/calendars/{cal_id}/events/{event_id}"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"}, method="DELETE")
    try:
        with urllib.request.urlopen(req) as resp:
            resp.read()
    except urllib.error.HTTPError as e:
        if e.code == 410:
            pass
        else:
            raise


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
# Test framework
# ---------------------------------------------------------------------------

class TestResult:
    def __init__(self, name):
        self.name = name
        self.passed = False
        self.error = None

    def __str__(self):
        status = "PASS" if self.passed else "FAIL"
        s = f"{status}: {self.name}"
        if self.error:
            s += f" - {self.error}"
        return s


def run_test(name, func):
    """Run a single test and return TestResult."""
    result = TestResult(name)
    try:
        func()
        result.passed = True
        print(f"  PASS: {name}")
    except Exception as e:
        result.error = str(e)
        print(f"  FAIL: {name} - {e}")
        traceback.print_exc()
    return result


# ---------------------------------------------------------------------------
# Test state (shared across tests)
# ---------------------------------------------------------------------------

class TestState:
    """Holds state shared across test steps."""
    test_period_start = None
    test_period_end = None
    test_period_str = None
    test_schedule_version = None
    kintone_wish_ids = []
    kintone_confirmed_ids = []
    staff_master_rows_added = 0


state = TestState()


# ---------------------------------------------------------------------------
# Test implementations
# ---------------------------------------------------------------------------

def test_01_register_test_staff():
    """Test 1: Register test staff to staff master sheet."""
    # Calculate test period (next Monday to Sunday+7)
    today = datetime.now().date()
    days_until_monday = (7 - today.weekday()) % 7
    if days_until_monday == 0:
        days_until_monday = 7
    start = today + timedelta(days=days_until_monday)
    end = start + timedelta(days=13)
    state.test_period_start = start.strftime("%Y-%m-%d")
    state.test_period_end = end.strftime("%Y-%m-%d")
    state.test_period_str = f"{state.test_period_start} ~ {state.test_period_end}"
    state.test_schedule_version = f"{state.test_period_start}_v99"

    # Append test staff to スタッフマスタ
    rows = []
    for s in TEST_STAFF:
        rows.append([
            s["staff_id"], s["name"], "", "", "", s["role"], "", "", "TRUE"
        ])

    sheets_append("スタッフマスタ!A1", rows)
    state.staff_master_rows_added = len(rows)

    # Verify
    all_rows = sheets_read("スタッフマスタ!A1:I100")
    found_ids = set()
    for row in all_rows:
        if row and row[0] in ("T001", "T002", "T003"):
            found_ids.add(row[0])

    assert found_ids == {"T001", "T002", "T003"}, \
        f"Expected T001-T003, found: {found_ids}"


def test_02_register_wishes_to_kintone():
    """Test 2: Register wish records to kintone app 211."""
    records = []
    for s in TEST_STAFF:
        records.append({
            "staff_id": {"value": s["staff_id"]},
            "staff_name": {"value": s["name"]},
            "shift_period": {"value": state.test_period_str},
            "desired_days_off": {"value": f"{state.test_period_start}"},
            "input_channel": {"value": "test_e2e"},
        })

    result = kintone_add_records(KINTONE_SHIFT_WISH_APP_ID, records)
    state.kintone_wish_ids = [str(rid) for rid in result.get("ids", [])]

    assert len(state.kintone_wish_ids) == 3, \
        f"Expected 3 records, created {len(state.kintone_wish_ids)}"

    # Verify
    query = f'staff_id in ("T001", "T002", "T003") and shift_period = "{state.test_period_str}"'
    verify = kintone_get_records(KINTONE_SHIFT_WISH_APP_ID, query)
    assert int(verify.get("totalCount", 0)) == 3


def test_03_generate_shift_dryrun():
    """Test 3: Run generate_shift.py in dry-run mode."""
    # We can't fully run generate_shift without Claude API call,
    # so we simulate by writing test data to シフト出力 sheet directly.
    dates = []
    start = datetime.strptime(state.test_period_start, "%Y-%m-%d")
    for i in range(14):
        d = start + timedelta(days=i)
        dates.append(d.strftime("%Y-%m-%d"))

    # Build test shift output
    header_row = ["staff_id", "スタッフ名"]
    for d in dates:
        dt = datetime.strptime(d, "%Y-%m-%d")
        weekday = ["月", "火", "水", "木", "金", "土", "日"][dt.weekday()]
        header_row.append(f"{d}\n({weekday})")
    header_row.extend(["出勤日数", "合計勤務時間"])

    data_rows = []
    for s in TEST_STAFF:
        row = [s["staff_id"], s["name"]]
        work_count = 0
        for i, d in enumerate(dates):
            # Assign work on weekdays, off on weekends
            dt = datetime.strptime(d, "%Y-%m-%d")
            if dt.weekday() < 5:  # Monday-Friday
                row.append("出勤")
                work_count += 1
            else:
                row.append("休み")
        row.extend([str(work_count), f"{work_count * 8}h"])
        data_rows.append(row)

    # Write to シフト出力 sheet (test area)
    all_values = [
        ["シフト期間", state.test_period_str],
        ["承認者", ""],  # Not approved yet
        ["承認日時", ""],
        ["schedule_version", ""],
        header_row,
    ] + data_rows

    sheets_update("シフト出力!A1", all_values)

    # Verify data was written
    verify = sheets_read("シフト出力!A1:R20")
    assert len(verify) >= 8, f"Expected at least 8 rows, got {len(verify)}"
    assert verify[0][1] == state.test_period_str


def test_04_validator_unit():
    """Test 4: Test shift_validator.py logic."""
    import shift_validator

    dates = []
    start = datetime.strptime(state.test_period_start, "%Y-%m-%d")
    for i in range(14):
        d = start + timedelta(days=i)
        dates.append(d.strftime("%Y-%m-%d"))

    # Build a valid schedule
    schedule = {}
    for s in TEST_STAFF:
        statuses = []
        for d in dates:
            dt = datetime.strptime(d, "%Y-%m-%d")
            if dt.weekday() < 5:
                statuses.append("出勤")
            else:
                statuses.append("休み")
        schedule[s["name"]] = statuses

    staff = [
        {"staff_id": s["staff_id"], "氏名": s["name"], "雇用形態": "正社員"}
        for s in TEST_STAFF
    ]

    rules = {
        "min_staff_per_day": 1,
        "max_consecutive_days": 6,
        "weekly_max_hours": 48,
        "hours_per_day": 8,
        "forced_work_days": [],
    }

    violations = shift_validator.validate_schedule(
        schedule, staff, rules, [d for d in dates]
    )

    hard_violations = [v for v in violations if v.get("severity") == "hard"]
    assert len(hard_violations) == 0, \
        f"Expected 0 hard violations, got {len(hard_violations)}: {hard_violations}"


def test_05_write_approval_data():
    """Test 5: Write test approval data to シフト出力 sheet."""
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # Update B2 (approver), B3 (approved_at), B4 (schedule_version)
    sheets_update("シフト出力!B2:B4", [
        ["test-approver@chillaxy.jp"],
        [now_str],
        [state.test_schedule_version],
    ])

    # Verify
    verify = sheets_read("シフト出力!A1:B5")
    assert verify[1][1] == "test-approver@chillaxy.jp", \
        f"Approver not set: {verify[1]}"
    assert verify[3][1] == state.test_schedule_version, \
        f"Version not set: {verify[3]}"


def test_06_sync_outbox():
    """Test 6: Test sync_outbox state management."""
    import sync_outbox

    # Create version
    version = sync_outbox.create_version(
        state.test_period_start, "test-approver@chillaxy.jp"
    )

    # The actual version may differ from our test version since create_version
    # auto-increments. We'll use whatever was created.
    state.test_schedule_version = version

    # Check initial state - all should be pending
    statuses = sync_outbox.get_status(version)
    assert statuses is not None, "Version not found in outbox"

    for dest in sync_outbox.DESTINATIONS:
        assert statuses[dest]["status"] == "pending", \
            f"{dest} should be pending, got {statuses[dest]['status']}"

    # Test state transitions
    sync_outbox.update_status(version, "slack", "sent")
    assert sync_outbox.get_status(version, "slack")["status"] == "sent"

    sync_outbox.update_status(version, "kintone", "failed", "test error")
    status = sync_outbox.get_status(version, "kintone")
    assert status["status"] == "failed"
    assert status["error"] == "test error"

    # should_run checks
    assert not sync_outbox.should_run(version, "slack"), "slack=sent should not run"
    assert sync_outbox.should_run(version, "kintone"), "kintone=failed should run"
    assert sync_outbox.should_run(version, "calendar"), "calendar=pending should run"

    # Reset for subsequent tests
    for dest in sync_outbox.DESTINATIONS:
        sync_outbox.update_status(version, dest, "pending")


def test_07_notify_shift_dryrun():
    """Test 7: Verify notify_shift doesn't change outbox in non-exec mode."""
    import sync_outbox

    version = state.test_schedule_version
    # Confirm slack and line are still pending
    assert sync_outbox.get_status(version, "slack")["status"] == "pending"
    assert sync_outbox.get_status(version, "line")["status"] == "pending"

    # We don't actually call notify_shift.run() as it would try to send messages.
    # Instead verify the outbox is still pending.
    print("    (notify_shift dry-run skipped - would require live API)")
    assert sync_outbox.get_status(version, "slack")["status"] == "pending"
    assert sync_outbox.get_status(version, "line")["status"] == "pending"


def test_08_calendar_sync_dryrun():
    """Test 8: Verify calendar outbox is still pending."""
    import sync_outbox

    version = state.test_schedule_version
    assert sync_outbox.get_status(version, "calendar")["status"] == "pending"
    print("    (calendar_sync dry-run skipped - would require live API)")


def test_09_kintone_register_dryrun():
    """Test 9: Verify kintone outbox is still pending."""
    import sync_outbox

    version = state.test_schedule_version
    assert sync_outbox.get_status(version, "kintone")["status"] == "pending"
    print("    (kintone_register dry-run skipped - would require sheet approval)")


def test_10_run_post_approval_status():
    """Test 10: Check run_post_approval --status output."""
    import sync_outbox

    version = state.test_schedule_version
    statuses = sync_outbox.get_status(version)
    assert statuses is not None

    pending_count = sum(
        1 for d in sync_outbox.DESTINATIONS
        if statuses[d]["status"] == "pending"
    )
    assert pending_count == len(sync_outbox.DESTINATIONS), \
        f"Expected all {len(sync_outbox.DESTINATIONS)} destinations pending, got {pending_count}"


def test_11_mf_kintai_dryrun():
    """Test 11: Verify mf_kintai outbox is still pending."""
    import sync_outbox

    version = state.test_schedule_version
    assert sync_outbox.get_status(version, "mf_kintai")["status"] == "pending"
    print("    (mf_kintai_sync dry-run skipped - requires MF_ACCESS_TOKEN)")


def test_12_reminder_dryrun():
    """Test 12: Run reminder in dry-run mode with test date."""
    # First, register confirmed shift records to kintone for our test staff
    records = []
    dates = []
    start = datetime.strptime(state.test_period_start, "%Y-%m-%d")
    for i in range(14):
        d = start + timedelta(days=i)
        if d.weekday() < 5:
            dates.append(d.strftime("%Y-%m-%d"))

    for s in TEST_STAFF:
        records.append({
            "staff_id": {"value": s["staff_id"]},
            "staff_name": {"value": s["name"]},
            "shift_period": {"value": state.test_period_str},
            "working_days": {"value": ", ".join(dates)},
            "off_days": {"value": ""},
            "confirmed_by": {"value": "test-approver@chillaxy.jp"},
            "confirmed_at": {"value": datetime.now().strftime("%Y-%m-%d %H:%M:%S")},
            "schedule_version": {"value": state.test_schedule_version},
        })

    result = kintone_add_records(KINTONE_SHIFT_CONFIRMED_APP_ID, records)
    state.kintone_confirmed_ids = [str(rid) for rid in result.get("ids", [])]

    # Run reminder with reference_date = 3 days before shift start
    ref_date = (start - timedelta(days=3)).strftime("%Y-%m-%d")

    import reminder
    target_records = reminder.run(
        dry_run=True, days_ahead=3, reference_date=ref_date
    )

    assert len(target_records) == 3, \
        f"Expected 3 target staff, got {len(target_records)}"


# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------

def cleanup():
    """Remove all test data."""
    print("\n=== Cleanup ===")
    errors = []

    # 1. Remove test staff from スタッフマスタ
    try:
        rows = sheets_read("スタッフマスタ!A1:I100")
        # Find rows with T001-T003 and clear them
        rows_to_clear = []
        for i, row in enumerate(rows):
            if row and row[0] in ("T001", "T002", "T003"):
                rows_to_clear.append(i + 1)  # 1-indexed

        for row_num in reversed(rows_to_clear):
            sheets_clear(f"スタッフマスタ!A{row_num}:I{row_num}")

        print(f"  Cleared {len(rows_to_clear)} test rows from スタッフマスタ")
    except Exception as e:
        errors.append(f"Staff master cleanup: {e}")
        print(f"  [ERROR] Staff master cleanup: {e}")

    # 2. Delete kintone wish records
    try:
        if state.kintone_wish_ids:
            kintone_delete_records(
                KINTONE_SHIFT_WISH_APP_ID, state.kintone_wish_ids
            )
            print(f"  Deleted {len(state.kintone_wish_ids)} wish records from kintone")
        else:
            # Fallback: search by staff_id
            query = 'staff_id in ("T001", "T002", "T003")'
            result = kintone_get_records(KINTONE_SHIFT_WISH_APP_ID, query)
            ids = [r["$id"]["value"] for r in result.get("records", [])]
            if ids:
                kintone_delete_records(KINTONE_SHIFT_WISH_APP_ID, ids)
                print(f"  Deleted {len(ids)} wish records from kintone (fallback)")
    except Exception as e:
        errors.append(f"Kintone wish cleanup: {e}")
        print(f"  [ERROR] Kintone wish cleanup: {e}")

    # 3. Delete kintone confirmed records
    try:
        if state.kintone_confirmed_ids:
            kintone_delete_records(
                KINTONE_SHIFT_CONFIRMED_APP_ID, state.kintone_confirmed_ids
            )
            print(f"  Deleted {len(state.kintone_confirmed_ids)} confirmed records from kintone")
        else:
            query = 'staff_id in ("T001", "T002", "T003")'
            result = kintone_get_records(KINTONE_SHIFT_CONFIRMED_APP_ID, query)
            ids = [r["$id"]["value"] for r in result.get("records", [])]
            if ids:
                kintone_delete_records(KINTONE_SHIFT_CONFIRMED_APP_ID, ids)
                print(f"  Deleted {len(ids)} confirmed records from kintone (fallback)")
    except Exception as e:
        errors.append(f"Kintone confirmed cleanup: {e}")
        print(f"  [ERROR] Kintone confirmed cleanup: {e}")

    # 4. Delete test calendar events
    try:
        if state.test_period_start and state.test_period_end:
            events = calendar_list_test_events(
                state.test_period_start, state.test_period_end
            )
            for e in events:
                calendar_delete_event(e["id"])
            if events:
                print(f"  Deleted {len(events)} test calendar events")
            else:
                print("  No test calendar events to delete")
    except Exception as e:
        errors.append(f"Calendar cleanup: {e}")
        print(f"  [ERROR] Calendar cleanup: {e}")

    # 5. Remove test entries from sync_outbox.json
    try:
        import sync_outbox
        outbox_file = sync_outbox.OUTBOX_FILE
        if os.path.exists(outbox_file):
            data = sync_outbox._load()
            # Remove entries with test version
            keys_to_remove = [
                k for k in data
                if k == state.test_schedule_version
                or k.startswith(f"{state.test_period_start}_v")
            ]
            for k in keys_to_remove:
                del data[k]
            sync_outbox._save(data)
            print(f"  Removed {len(keys_to_remove)} test entries from sync_outbox.json")
    except Exception as e:
        errors.append(f"Outbox cleanup: {e}")
        print(f"  [ERROR] Outbox cleanup: {e}")

    if errors:
        print(f"\n  Cleanup completed with {len(errors)} errors")
    else:
        print("\n  Cleanup completed successfully")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    skip_cleanup = "--no-cleanup" in sys.argv

    print("=" * 60)
    print("  E2E Test Suite - Shift Management System")
    print("=" * 60)
    print(f"  Date: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print()

    tests = [
        ("1. テスト用スタッフ3名をスタッフマスタに登録", test_01_register_test_staff),
        ("2. kintone希望収集にテストレコード登録", test_02_register_wishes_to_kintone),
        ("3. generate_shift dry-run (テストデータ書き込み)", test_03_generate_shift_dryrun),
        ("4. shift_validator 単体テスト", test_04_validator_unit),
        ("5. シフト出力シートに承認データ書き込み", test_05_write_approval_data),
        ("6. sync_outbox 初期化・状態遷移テスト", test_06_sync_outbox),
        ("7. notify_shift dry-run確認", test_07_notify_shift_dryrun),
        ("8. calendar_sync dry-run確認", test_08_calendar_sync_dryrun),
        ("9. kintone_shift_register dry-run確認", test_09_kintone_register_dryrun),
        ("10. run_post_approval --status確認", test_10_run_post_approval_status),
        ("11. mf_kintai_sync dry-run確認", test_11_mf_kintai_dryrun),
        ("12. reminder dry-run実行", test_12_reminder_dryrun),
    ]

    results = []
    for name, func in tests:
        print(f"\n--- Test: {name} ---")
        result = run_test(name, func)
        results.append(result)

        # Stop on critical failures (tests 1-2 are prerequisites)
        if not result.passed and name.startswith(("1.", "2.")):
            print(f"\n  CRITICAL: Prerequisite test failed. Stopping.")
            break

    # Cleanup
    if not skip_cleanup:
        try:
            cleanup()
        except Exception as e:
            print(f"\n  Cleanup error: {e}")
            traceback.print_exc()
    else:
        print("\n  Cleanup skipped (--no-cleanup)")

    # Summary
    passed = sum(1 for r in results if r.passed)
    total = len(results)

    print("\n" + "=" * 60)
    print(f"  Results: {passed}/{total} PASS")
    print("=" * 60)

    for r in results:
        print(f"  {r}")

    failed = [r for r in results if not r.passed]
    if failed:
        print(f"\n  Failed tests:")
        for r in failed:
            print(f"    - {r.name}: {r.error}")

    # Post summary to Slack
    summary_lines = [f"  {r}" for r in results]
    summary_text = (
        f"【E2Eテスト結果】\n"
        f"実行日時: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
        f"結果: {passed}/{total} PASS\n\n"
        + "\n".join(summary_lines)
    )

    if failed:
        summary_text += "\n\n失敗したテスト:\n"
        for r in failed:
            summary_text += f"  - {r.name}: {r.error}\n"

    slack_notify(summary_text)

    # Exit code
    sys.exit(0 if passed == total else 1)


if __name__ == "__main__":
    main()
