#!/usr/bin/env python3
"""Integration tests for shift preference collection (3 routes).

Tests (updated for 1-record-per-day kintone 211 schema):
  (1) Slack reply parse -> kintone app 211 registration (1 record per day)
  (2) LINE message parse -> kintone app 211 registration (1 record per day)
  (3) Direct kintone registration (1 record per day)

Each test verifies input_channel, staff_id, shift_date, shift_type fields.
Errors are reported to Slack admin DM.

Usage:
    python3 test_integration.py           # Run all tests
    python3 test_integration.py slack     # Run Slack route only
    python3 test_integration.py line      # Run LINE route only
    python3 test_integration.py kintone   # Run kintone direct only
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
    KINTONE_SHIFT_WISH_APP_ID,
    SLACK_BOT_TOKEN, SLACK_SHIFT_CHANNEL,
)

# Test identifier to find/cleanup test records
TEST_PREFIX = "__TEST__"
TEST_STAFF_ID = "T999"
TEST_PERIOD_START = "2099-01-01"
TEST_PERIOD_END = "2099-01-14"
TEST_DATES = ["2099-01-05", "2099-01-10"]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def kintone_api(path, method="GET", body=None):
    url = f"https://{KINTONE_DOMAIN}/k/v1/{path}"
    credential = base64.b64encode(
        f"{KINTONE_USERNAME}:{KINTONE_PASSWORD}".encode()
    ).decode()
    headers = {"X-Cybozu-Authorization": credential}
    if body:
        headers["Content-Type"] = "application/json"
        data = json.dumps(body).encode()
    else:
        data = None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read())


def kintone_add_day_records(staff_id, staff_name, dates, input_channel, remarks=""):
    """Register 1 record per date (new kintone 211 schema)."""
    records = []
    for d in dates:
        records.append({
            "staff_id": {"value": staff_id},
            "staff_name": {"value": staff_name},
            "shift_date": {"value": d},
            "shift_type": {"value": "休み"},
            "work_time_type": {"value": "フリー"},
            "start_time": {"value": ""},
            "end_time": {"value": ""},
            "input_status": {"value": "入力済"},
            "input_channel": {"value": input_channel},
            "target_period_start": {"value": TEST_PERIOD_START},
            "target_period_end": {"value": TEST_PERIOD_END},
            "remarks": {"value": remarks},
        })
    result = kintone_api("records.json", "POST", {
        "app": KINTONE_SHIFT_WISH_APP_ID,
        "records": records,
    })
    return [str(rid) for rid in result.get("ids", [])]


def kintone_get_record(record_id):
    body = {"app": KINTONE_SHIFT_WISH_APP_ID, "id": record_id}
    data = json.dumps(body).encode()
    credential = base64.b64encode(
        f"{KINTONE_USERNAME}:{KINTONE_PASSWORD}".encode()
    ).decode()
    headers = {
        "X-Cybozu-Authorization": credential,
        "Content-Type": "application/json",
    }
    url = f"https://{KINTONE_DOMAIN}/k/v1/record.json"
    req = urllib.request.Request(url, data=data, headers=headers, method="GET")
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read())


def kintone_delete_records(record_ids):
    if not record_ids:
        return
    # Delete in batches of 100
    for i in range(0, len(record_ids), 100):
        batch = [int(rid) for rid in record_ids[i:i+100]]
        kintone_api("records.json", "DELETE", {
            "app": KINTONE_SHIFT_WISH_APP_ID,
            "ids": batch,
        })


def kintone_find_test_records():
    query = f'staff_id = "{TEST_STAFF_ID}" and target_period_start = "{TEST_PERIOD_START}"'
    body = {"app": KINTONE_SHIFT_WISH_APP_ID, "query": query, "totalCount": True}
    data = json.dumps(body).encode()
    credential = base64.b64encode(
        f"{KINTONE_USERNAME}:{KINTONE_PASSWORD}".encode()
    ).decode()
    headers = {
        "X-Cybozu-Authorization": credential,
        "Content-Type": "application/json",
    }
    url = f"https://{KINTONE_DOMAIN}/k/v1/records.json"
    req = urllib.request.Request(url, data=data, headers=headers, method="GET")
    with urllib.request.urlopen(req) as resp:
        result = json.loads(resp.read())
    return result.get("records", [])


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
        print(f"Slack error notify failed: {e}")


# ---------------------------------------------------------------------------
# Test results tracking
# ---------------------------------------------------------------------------

class TestResult:
    def __init__(self):
        self.passed = 0
        self.failed = 0
        self.errors = []
        self._cleanup_ids = []

    def track_records(self, record_ids):
        self._cleanup_ids.extend(record_ids)

    def ok(self, name):
        self.passed += 1
        print(f"  PASS: {name}")

    def fail(self, name, detail):
        self.failed += 1
        self.errors.append((name, detail))
        print(f"  FAIL: {name} - {detail}")

    def cleanup(self):
        if self._cleanup_ids:
            try:
                kintone_delete_records(self._cleanup_ids)
                print(f"\nCleanup: deleted {len(self._cleanup_ids)} test records")
            except Exception as e:
                print(f"\nCleanup warning: {e}")

    def summary(self):
        total = self.passed + self.failed
        print(f"\n{'='*50}")
        print(f"Results: {self.passed}/{total} passed, {self.failed} failed")
        if self.errors:
            print("Failures:")
            for name, detail in self.errors:
                print(f"  - {name}: {detail}")
        print(f"{'='*50}")
        return self.failed == 0


# ---------------------------------------------------------------------------
# Test (1): Slack thread reply parse -> kintone
# ---------------------------------------------------------------------------

def test_slack_route(result: TestResult):
    """Test Slack message parsing and kintone registration."""
    print("\n[Test 1] Slack route: parse reply -> kintone (1 record per day)")
    location = "test_slack_route"

    try:
        from slack_shift_bot import parse_shift_reply

        # 1a. Parse standard format
        days_off, remarks = parse_shift_reply("希望休: 2099-01-05, 2099-01-10\n備考: テスト備考")
        if days_off != "2099-01-05, 2099-01-10":
            result.fail("slack_parse_days_off", f"expected '2099-01-05, 2099-01-10', got '{days_off}'")
            return
        result.ok("slack_parse_days_off")

        if remarks != "テスト備考":
            result.fail("slack_parse_remarks", f"expected 'テスト備考', got '{remarks}'")
            return
        result.ok("slack_parse_remarks")

        # 1b. Register parsed dates as individual records to kintone
        staff_name = f"{TEST_PREFIX}SlackUser"
        parsed_dates = [d.strip() for d in days_off.split(",")]
        record_ids = kintone_add_day_records(
            TEST_STAFF_ID, staff_name, parsed_dates, "Slack", remarks
        )
        if len(record_ids) != len(parsed_dates):
            result.fail("slack_kintone_add", f"expected {len(parsed_dates)} records, got {len(record_ids)}")
            return
        result.track_records(record_ids)
        result.ok("slack_kintone_add")

        # 1c. Verify first registered record
        record = kintone_get_record(record_ids[0])
        rec = record["record"]

        if rec["input_channel"]["value"] != "Slack":
            result.fail("slack_input_channel", f"expected 'Slack', got '{rec['input_channel']['value']}'")
            return
        result.ok("slack_input_channel")

        if rec["staff_id"]["value"] != TEST_STAFF_ID:
            result.fail("slack_staff_id", f"expected '{TEST_STAFF_ID}', got '{rec['staff_id']['value']}'")
            return
        result.ok("slack_staff_id")

        if rec["shift_date"]["value"] != "2099-01-05":
            result.fail("slack_shift_date", f"expected '2099-01-05', got '{rec['shift_date']['value']}'")
            return
        result.ok("slack_shift_date")

        if rec["shift_type"]["value"] != "休み":
            result.fail("slack_shift_type", f"expected '休み', got '{rec['shift_type']['value']}'")
            return
        result.ok("slack_shift_type")

        if rec["input_status"]["value"] != "入力済":
            result.fail("slack_input_status", f"expected '入力済', got '{rec['input_status']['value']}'")
            return
        result.ok("slack_input_status")

    except Exception as e:
        detail = str(e)
        result.fail(location, detail)
        slack_notify_error(location, detail)


# ---------------------------------------------------------------------------
# Test (2): LINE message parse -> kintone (input_channel=LINE)
# ---------------------------------------------------------------------------

def test_line_route(result: TestResult):
    """Test LINE message parsing and kintone registration."""
    print("\n[Test 2] LINE route: parse message -> kintone (1 record per day)")
    location = "test_line_route"

    try:
        from line_bot import parse_line_message

        # 2a. Parse LINE format "希望休 1/5, 1/10" (returns list of dates)
        days_off, remarks = parse_line_message("希望休 1/5, 1/10")
        if days_off is None:
            result.fail("line_parse", "parse returned None")
            return
        # days_off is now a list of YYYY-MM-DD strings
        if not isinstance(days_off, list):
            result.fail("line_parse_type", f"expected list, got {type(days_off)}")
            return
        for d in days_off:
            if not (len(d) == 10 and d[4] == "-" and d[7] == "-"):
                result.fail("line_date_normalize", f"date not YYYY-MM-DD format: '{d}'")
                return
        result.ok("line_parse_and_normalize")

        # 2b. Parse with remarks
        _, remarks2 = parse_line_message("希望休 4/5, 4/12\n備考 午前のみ希望(4/8)")
        if remarks2 != "午前のみ希望(4/8)":
            result.fail("line_parse_remarks", f"expected '午前のみ希望(4/8)', got '{remarks2}'")
            return
        result.ok("line_parse_remarks")

        # 2c. Non-shift message should return None
        none_days, _ = parse_line_message("おはようございます")
        if none_days is not None:
            result.fail("line_parse_non_shift", f"expected None, got '{none_days}'")
            return
        result.ok("line_parse_non_shift")

        # 2d. Register to kintone with input_channel=LINE (use fixed test dates)
        staff_name = f"{TEST_PREFIX}LINEUser"
        record_ids = kintone_add_day_records(
            TEST_STAFF_ID, staff_name, TEST_DATES, "LINE", "テストLINE備考"
        )
        if len(record_ids) != len(TEST_DATES):
            result.fail("line_kintone_add", f"expected {len(TEST_DATES)} records, got {len(record_ids)}")
            return
        result.track_records(record_ids)
        result.ok("line_kintone_add")

        # 2e. Verify record fields
        record = kintone_get_record(record_ids[0])
        rec = record["record"]

        if rec["input_channel"]["value"] != "LINE":
            result.fail("line_input_channel", f"expected 'LINE', got '{rec['input_channel']['value']}'")
            return
        result.ok("line_input_channel")

        if rec["staff_id"]["value"] != TEST_STAFF_ID:
            result.fail("line_staff_id", f"expected '{TEST_STAFF_ID}', got '{rec['staff_id']['value']}'")
            return
        result.ok("line_staff_id")

        if rec["shift_date"]["value"] != TEST_DATES[0]:
            result.fail("line_shift_date", f"expected '{TEST_DATES[0]}', got '{rec['shift_date']['value']}'")
            return
        result.ok("line_shift_date")

    except Exception as e:
        detail = str(e)
        result.fail(location, detail)
        slack_notify_error(location, detail)


# ---------------------------------------------------------------------------
# Test (3): Direct kintone registration
# ---------------------------------------------------------------------------

def test_kintone_direct(result: TestResult):
    """Test direct kintone record creation and field verification."""
    print("\n[Test 3] kintone direct registration (1 record per day)")
    location = "test_kintone_direct"

    try:
        staff_name = f"{TEST_PREFIX}DirectUser"
        test_dates = ["2099-01-03", "2099-01-07", "2099-01-14"]
        input_channel = "kintone直接"

        # 3a. Create records (1 per day)
        record_ids = kintone_add_day_records(
            TEST_STAFF_ID, staff_name, test_dates, input_channel, "直接登録テスト"
        )
        if len(record_ids) != len(test_dates):
            result.fail("kintone_direct_add", f"expected {len(test_dates)} records, got {len(record_ids)}")
            return
        result.track_records(record_ids)
        result.ok("kintone_direct_add")

        # 3b. Read back and verify all fields on first record
        record = kintone_get_record(record_ids[0])
        rec = record["record"]

        checks = [
            ("kintone_direct_input_channel", "input_channel", input_channel),
            ("kintone_direct_staff_id", "staff_id", TEST_STAFF_ID),
            ("kintone_direct_staff_name", "staff_name", staff_name),
            ("kintone_direct_shift_date", "shift_date", test_dates[0]),
            ("kintone_direct_shift_type", "shift_type", "休み"),
            ("kintone_direct_input_status", "input_status", "入力済"),
            ("kintone_direct_remarks", "remarks", "直接登録テスト"),
            ("kintone_direct_period_start", "target_period_start", TEST_PERIOD_START),
            ("kintone_direct_period_end", "target_period_end", TEST_PERIOD_END),
        ]
        for check_name, field, expected in checks:
            actual = rec[field]["value"]
            if actual != expected:
                result.fail(check_name, f"expected '{expected}', got '{actual}'")
                return
            result.ok(check_name)

        # 3c. Verify correct number of records created
        found = kintone_find_test_records()
        # May include records from other tests; check at least our 3
        found_dates = sorted(r["shift_date"]["value"] for r in found)
        for d in test_dates:
            if d not in found_dates:
                result.fail("kintone_direct_date_check", f"date {d} not found in records")
                return
        result.ok("kintone_direct_all_dates_found")

    except Exception as e:
        detail = str(e)
        result.fail(location, detail)
        slack_notify_error(location, detail)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

TESTS = {
    "slack": test_slack_route,
    "line": test_line_route,
    "kintone": test_kintone_direct,
}


def main():
    # Determine which tests to run
    if len(sys.argv) > 1:
        names = sys.argv[1:]
        for n in names:
            if n not in TESTS:
                print(f"Unknown test: {n}")
                print(f"Available: {', '.join(TESTS.keys())}")
                sys.exit(1)
    else:
        names = list(TESTS.keys())

    print(f"Integration Test - shift management ({', '.join(names)})")
    print(f"kintone app: {KINTONE_SHIFT_WISH_APP_ID} @ {KINTONE_DOMAIN}")
    print(f"Test period: {TEST_PERIOD_START} ~ {TEST_PERIOD_END}")
    print(f"Schema: 1 record per staff per day")

    # Cleanup any leftover test records
    try:
        old = kintone_find_test_records()
        if old:
            old_ids = [r["$id"]["value"] for r in old]
            kintone_delete_records(old_ids)
            print(f"Cleaned up {len(old_ids)} leftover test records")
    except Exception as e:
        print(f"Pre-cleanup warning: {e}")

    result = TestResult()

    for name in names:
        TESTS[name](result)

    result.cleanup()
    success = result.summary()

    if not success:
        failed_names = [n for n, _ in result.errors]
        slack_notify_error(
            "test_integration",
            f"Failed tests: {', '.join(failed_names)} ({result.failed}/{result.passed + result.failed})"
        )

    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
