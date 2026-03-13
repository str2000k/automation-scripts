#!/usr/bin/env python3
"""希望収集レコード一括生成スクリプト。

kintone ID=213（スタッフマスタ）から shift_type=希望シフト のスタッフを取得し、
kintone ID=211（希望収集）に対象期間の全日×全対象スタッフのレコードを一括生成する。

固定シフト・混合のスタッフは生成しない。

Usage:
    python3 init_shift_requests.py                                          # dry-run, 来月1-14日
    python3 init_shift_requests.py --period-start 2026-04-01 --period-end 2026-04-14
    python3 init_shift_requests.py --period-start 2026-04-01 --period-end 2026-04-14 --no-dry-run
"""
import argparse
import base64
import datetime
import json
import sys
import urllib.error
import urllib.parse
import urllib.request

from config import (
    KINTONE_DOMAIN, KINTONE_USERNAME, KINTONE_PASSWORD,
    KINTONE_SHIFT_WISH_APP_ID, KINTONE_STAFF_MASTER_APP_ID,
    SLACK_BOT_TOKEN, SLACK_SHIFT_CHANNEL,
)


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


def kintone_get_records(app_id, query, fields=None):
    """Fetch records from kintone app. Returns list of record dicts."""
    headers = _kintone_headers()
    url = f"https://{KINTONE_DOMAIN}/k/v1/records.json"
    all_records = []
    offset = 0
    while True:
        body = {
            "app": app_id,
            "query": f"{query} limit 500 offset {offset}",
        }
        if fields:
            body["fields"] = fields
        data = json.dumps(body).encode()
        req = urllib.request.Request(url, data=data, headers=headers, method="GET")
        try:
            with urllib.request.urlopen(req) as resp:
                result = json.loads(resp.read())
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"kintone GET {e.code}: {err_body}") from e
        records = result.get("records", [])
        all_records.extend(records)
        if len(records) < 500:
            break
        offset += 500
    return all_records


def kintone_add_records(app_id, records):
    """Add records to kintone app. Max 100 per call."""
    headers = _kintone_headers()
    url = f"https://{KINTONE_DOMAIN}/k/v1/records.json"
    body = {"app": app_id, "records": records}
    data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"kintone POST {e.code}: {err_body}") from e


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
# Core logic
# ---------------------------------------------------------------------------

def get_required_staff():
    """kintone ID=213 から shift_type=希望シフト かつ active=有効 のスタッフを取得。"""
    records = kintone_get_records(
        KINTONE_STAFF_MASTER_APP_ID,
        query='shift_type in ("希望シフト") and active in ("有効") order by staff_id asc',
        fields=["staff_id", "name", "slack_id", "line_uid"],
    )
    staff_list = []
    for r in records:
        staff_list.append({
            "staff_id": r["staff_id"]["value"],
            "name": r["name"]["value"],
            "slack_id": r.get("slack_id", {}).get("value", ""),
            "line_uid": r.get("line_uid", {}).get("value", ""),
        })
    return staff_list


def get_existing_record_keys(period_start, period_end):
    """ID=211 の既存レコードの (staff_id, shift_date) セットを返す（重複防止）。"""
    records = kintone_get_records(
        KINTONE_SHIFT_WISH_APP_ID,
        query=f'target_period_start = "{period_start}" and target_period_end = "{period_end}" order by staff_id asc',
        fields=["staff_id", "shift_date"],
    )
    keys = set()
    for r in records:
        sid = r.get("staff_id", {}).get("value", "")
        sd = r.get("shift_date", {}).get("value", "")
        if sid and sd:
            keys.add((sid, sd))
    return keys


def generate_date_range(start, end):
    """開始日〜終了日の日付リストを返す。"""
    dates = []
    current = datetime.date.fromisoformat(start)
    end_date = datetime.date.fromisoformat(end)
    while current <= end_date:
        dates.append(str(current))
        current += datetime.timedelta(days=1)
    return dates


def init_shift_requests(period_start, period_end, dry_run=True):
    """希望収集レコードを一括生成する。"""
    print("=== 希望収集レコード一括生成 ===")
    print(f"  対象期間: {period_start} 〜 {period_end}")
    print(f"  モード: {'DRY-RUN' if dry_run else '本番実行'}")
    print(f"  kintone App ID: {KINTONE_SHIFT_WISH_APP_ID} (希望収集)")
    print()

    # Validate app IDs
    if not KINTONE_SHIFT_WISH_APP_ID:
        raise RuntimeError("KINTONE_SHIFT_WISH_APP_ID not set")
    if not KINTONE_STAFF_MASTER_APP_ID:
        raise RuntimeError("KINTONE_STAFF_MASTER_APP_ID not set")

    # (1) 希望シフト対象スタッフを取得
    print("[1/4] 希望シフト対象スタッフを取得...")
    staff_list = get_required_staff()
    print(f"  対象スタッフ: {len(staff_list)}名")
    for s in staff_list:
        print(f"    - {s['staff_id']}: {s['name']}")
    print()

    if not staff_list:
        print("  対象スタッフがいません。終了します。")
        return 0

    # (2) 日付リストを生成
    dates = generate_date_range(period_start, period_end)
    print(f"[2/4] 対象日数: {len(dates)}日")

    # (3) 既存レコードを取得（重複防止）
    print("[3/4] 既存レコードを確認...")
    existing_keys = get_existing_record_keys(period_start, period_end)
    print(f"  既存レコード: {len(existing_keys)}件（スキップ対象）")
    print()

    # (4) 生成するレコードを構築
    records_to_create = []
    skipped = 0
    for date in dates:
        for staff in staff_list:
            key = (staff["staff_id"], date)
            if key in existing_keys:
                skipped += 1
                continue
            records_to_create.append({
                "staff_id": {"value": staff["staff_id"]},
                "staff_name": {"value": staff["name"]},
                "shift_date": {"value": date},
                "shift_type": {"value": "出勤"},
                "work_time_type": {"value": "フリー"},
                "start_time": {"value": ""},
                "end_time": {"value": ""},
                "input_status": {"value": "未入力"},
                "target_period_start": {"value": period_start},
                "target_period_end": {"value": period_end},
                "input_channel": {"value": ""},
                "remarks": {"value": ""},
            })

    print(f"[4/4] レコード生成")
    print(f"  生成予定: {len(records_to_create)}件")
    print(f"  スキップ: {skipped}件（既存）")
    print()

    if not records_to_create:
        print("  生成するレコードがありません。")
        return 0

    if dry_run:
        print("[DRY-RUN] 実際には書き込みません。--no-dry-run で本番実行してください。")
        print()
        print("サンプル（先頭3件）:")
        for r in records_to_create[:3]:
            print(f"  {r['shift_date']['value']} / {r['staff_id']['value']} {r['staff_name']['value']} / {r['input_status']['value']}")
        if len(records_to_create) > 3:
            print(f"  ... 他 {len(records_to_create) - 3}件")
        return len(records_to_create)

    # 本番登録（100件バッチ）
    total_created = 0
    for i in range(0, len(records_to_create), 100):
        batch = records_to_create[i : i + 100]
        kintone_add_records(KINTONE_SHIFT_WISH_APP_ID, batch)
        total_created += len(batch)
        print(f"  登録完了: {total_created}/{len(records_to_create)}件")

    print(f"\n  完了: {total_created}件登録しました")

    slack_notify(
        f"希望収集レコード一括生成完了\n"
        f"対象期間: {period_start} 〜 {period_end}\n"
        f"対象スタッフ: {len(staff_list)}名\n"
        f"生成件数: {total_created}件"
    )

    return total_created


def main():
    parser = argparse.ArgumentParser(description="希望収集レコード一括生成")

    # デフォルトは来月1日〜14日
    today = datetime.date.today()
    next_month = (today.replace(day=1) + datetime.timedelta(days=32)).replace(day=1)
    default_start = str(next_month)
    default_end = str(next_month + datetime.timedelta(days=13))

    parser.add_argument("--period-start", default=default_start,
                        help=f"対象期間開始日 YYYY-MM-DD (デフォルト: {default_start})")
    parser.add_argument("--period-end", default=default_end,
                        help=f"対象期間終了日 YYYY-MM-DD (デフォルト: {default_end})")
    parser.add_argument("--no-dry-run", action="store_true",
                        help="本番実行（省略時はdry-run）")

    args = parser.parse_args()

    init_shift_requests(
        period_start=args.period_start,
        period_end=args.period_end,
        dry_run=not args.no_dry_run,
    )


if __name__ == "__main__":
    main()
