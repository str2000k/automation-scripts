#!/usr/bin/env python3
"""STEP 5: Integration tests for shift preference collection (3 routes).

Tests:
  (1) Slack thread reply parse -> kintone app 211 registration
  (2) LINE message parse -> kintone app 211 registration (input_channel=LINE)
  (3) Direct kintone registration

Each test verifies input_channel, staff_name, desired_days_off fields.
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
TEST_PERIOD_START = "2099-01-01"
TEST_PERIOD_END = "2099-01-14"


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


def kintone_add_record(staff_name, days_off, input_channel, remarks=""):
    now = datetime.now().strftime("%Y-%m-%dT%H:%M:%S+09:00")
    record = {
        "staff_name": {"value": staff_name},
        "target_period_start": {"value": TEST_PERIOD_START},
        "target_period_end": {"value": TEST_PERIOD_END},
        "desired_days_off": {"value": days_off},
        "remarks": {"value": remarks},
        "input_channel": {"value": input_channel},
        "submitted_at": {"value": now},
        "status": {"value": "未処理"},
    }
    return kintone_api("record.json", "POST", {
        "app": KINTONE_SHIFT_WISH_APP_ID,
        "record": record,
    })


def kintone_get_record(record_id):
    return kintone_api(
        f"record.json?app={KINTONE_SHIFT_WISH_APP_ID}&id={record_id}"
    )


def kintone_delete_records(record_ids):
    if not record_ids:
        return
    kintone_api("records.json", "DELETE", {
        "app": KINTONE_SHIFT_WISH_APP_ID,
        "ids": record_ids,
    })


def kintone_find_test_records():
    query = f'staff_name like "{TEST_PREFIX}" and target_period_start = "{TEST_PERIOD_START}"'
    result = kintone_api(
        f"records.json?app={KINTONE_SHIFT_WISH_APP_ID}&query={urllib.parse.quote(query)}"
    )
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

    def track_record(self, record_id):
        self._cleanup_ids.append(int(record_id))

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
    print("\n[Test 1] Slack route: parse reply -> kintone")
    location = "test_slack_route"

    try:
        # Import parser from slack_shift_bot
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

        # 1b. Register parsed result to kintone
        staff_name = f"{TEST_PREFIX}SlackUser"
        resp = kintone_add_record(staff_name, days_off, "Slack", remarks)
        record_id = resp.get("id")
        if not record_id:
            result.fail("slack_kintone_add", f"no record id returned: {resp}")
            return
        result.track_record(record_id)
        result.ok("slack_kintone_add")

        # 1c. Verify registered record
        record = kintone_get_record(record_id)
        rec = record["record"]

        if rec["input_channel"]["value"] != "Slack":
            result.fail("slack_input_channel", f"expected 'Slack', got '{rec['input_channel']['value']}'")
            return
        result.ok("slack_input_channel")

        if rec["staff_name"]["value"] != staff_name:
            result.fail("slack_staff_name", f"expected '{staff_name}', got '{rec['staff_name']['value']}'")
            return
        result.ok("slack_staff_name")

        if rec["desired_days_off"]["value"] != "2099-01-05, 2099-01-10":
            result.fail("slack_desired_days_off", f"got '{rec['desired_days_off']['value']}'")
            return
        result.ok("slack_desired_days_off")

    except Exception as e:
        detail = str(e)
        result.fail(location, detail)
        slack_notify_error(location, detail)


# ---------------------------------------------------------------------------
# Test (2): LINE message parse -> kintone (input_channel=LINE)
# ---------------------------------------------------------------------------

def test_line_route(result: TestResult):
    """Test LINE message parsing and kintone registration."""
    print("\n[Test 2] LINE route: parse message -> kintone")
    location = "test_line_route"

    try:
        from line_bot import parse_line_message

        # 2a. Parse LINE format "希望休 4/5, 4/12"
        days_off, remarks = parse_line_message("希望休 4/5, 4/12")
        if days_off is None:
            result.fail("line_parse", "parse returned None")
            return
        # Verify dates are normalized to YYYY-MM-DD
        parts = [d.strip() for d in days_off.split(",")]
        for p in parts:
            if not (len(p) == 10 and p[4] == "-" and p[7] == "-"):
                result.fail("line_date_normalize", f"date not YYYY-MM-DD format: '{p}'")
                return
        result.ok("line_parse_and_normalize")

        # 2b. Parse with remarks
        days_off2, remarks2 = parse_line_message("希望休 4/5, 4/12\n備考 午前のみ希望(4/8)")
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

        # 2d. Register to kintone with input_channel=LINE
        staff_name = f"{TEST_PREFIX}LINEUser"
        test_days = "2099-01-05, 2099-01-12"
        resp = kintone_add_record(staff_name, test_days, "LINE", "テストLINE備考")
        record_id = resp.get("id")
        if not record_id:
            result.fail("line_kintone_add", f"no record id returned: {resp}")
            return
        result.track_record(record_id)
        result.ok("line_kintone_add")

        # 2e. Verify record fields
        record = kintone_get_record(record_id)
        rec = record["record"]

        if rec["input_channel"]["value"] != "LINE":
            result.fail("line_input_channel", f"expected 'LINE', got '{rec['input_channel']['value']}'")
            return
        result.ok("line_input_channel")

        if rec["staff_name"]["value"] != staff_name:
            result.fail("line_staff_name", f"expected '{staff_name}', got '{rec['staff_name']['value']}'")
            return
        result.ok("line_staff_name")

        if rec["desired_days_off"]["value"] != test_days:
            result.fail("line_desired_days_off", f"expected '{test_days}', got '{rec['desired_days_off']['value']}'")
            return
        result.ok("line_desired_days_off")

    except Exception as e:
        detail = str(e)
        result.fail(location, detail)
        slack_notify_error(location, detail)


# ---------------------------------------------------------------------------
# Test (3): Direct kintone registration
# ---------------------------------------------------------------------------

def test_kintone_direct(result: TestResult):
    """Test direct kintone record creation and field verification."""
    print("\n[Test 3] kintone direct registration")
    location = "test_kintone_direct"

    try:
        staff_name = f"{TEST_PREFIX}DirectUser"
        days_off = "2099-01-03, 2099-01-07, 2099-01-14"
        input_channel = "kintone直接"

        # 3a. Create record
        resp = kintone_add_record(staff_name, days_off, input_channel, "直接登録テスト")
        record_id = resp.get("id")
        if not record_id:
            result.fail("kintone_direct_add", f"no record id returned: {resp}")
            return
        result.track_record(record_id)
        result.ok("kintone_direct_add")

        # 3b. Read back and verify all fields
        record = kintone_get_record(record_id)
        rec = record["record"]

        checks = [
            ("kintone_direct_input_channel", "input_channel", input_channel),
            ("kintone_direct_staff_name", "staff_name", staff_name),
            ("kintone_direct_desired_days_off", "desired_days_off", days_off),
            ("kintone_direct_remarks", "remarks", "直接登録テスト"),
            ("kintone_direct_status", "status", "未処理"),
            ("kintone_direct_period_start", "target_period_start", TEST_PERIOD_START),
            ("kintone_direct_period_end", "target_period_end", TEST_PERIOD_END),
        ]
        for check_name, field, expected in checks:
            actual = rec[field]["value"]
            if actual != expected:
                result.fail(check_name, f"expected '{expected}', got '{actual}'")
                return
            result.ok(check_name)

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

    # Cleanup any leftover test records
    try:
        old = kintone_find_test_records()
        if old:
            old_ids = [int(r["$id"]["value"]) for r in old]
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
        # Notify Slack of overall failure
        failed_names = [n for n, _ in result.errors]
        slack_notify_error(
            "test_integration",
            f"Failed tests: {', '.join(failed_names)} ({result.failed}/{result.passed + result.failed})"
        )

    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
