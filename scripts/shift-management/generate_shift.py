#!/usr/bin/env python3
"""STEP 6: AI shift auto-generation.

Processing order:
  (1) Read staff master & rule master from spreadsheet
  (2) Fetch wish records from kintone app 211 for target period
  (3) Write wish data to '希望収集データ' sheet (grid format)
  (4) Call Claude API to generate shift schedule (store-based)
  (5) Write results to 'シフト出力' sheet (store-based layout)

Output sheet layout matches the reference spreadsheet format:
  - Store assignment section (早番/遅番 per retail store)
  - Individual time schedule section (per staff)

Usage:
    python3 generate_shift.py                    # Auto-detect next period
    python3 generate_shift.py 2026-03-09 2026-03-22  # Specify period
"""
import base64
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta

from config import (
    KINTONE_DOMAIN, KINTONE_USERNAME, KINTONE_PASSWORD,
    KINTONE_SHIFT_WISH_APP_ID, SHIFT_SPREADSHEET_ID,
    SLACK_BOT_TOKEN, SLACK_SHIFT_CHANNEL,
    ANTHROPIC_API_KEY, STAFF_ID_COLUMN,
)

GOOGLE_EMAIL = "satoru@chillaxy.jp"
SHEET_ID_OUTPUT = 1533022256  # シフト出力 sheet id
SHEET_ID_WISH = 1764958489    # 希望収集データ sheet id


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

def col_letter(idx):
    """Convert 0-based column index to spreadsheet letter(s). 0=A, 25=Z, 26=AA."""
    result = ""
    idx_copy = idx
    while idx_copy >= 0:
        result = chr(ord("A") + idx_copy % 26) + result
        idx_copy = idx_copy // 26 - 1
    return result


# ---------------------------------------------------------------------------
# API helpers
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


def sheets_read(range_name):
    """Read from Google Sheets."""
    cred_path = _find_google_credential()
    if not cred_path:
        raise RuntimeError("No Google OAuth credential found")

    token = _get_access_token(cred_path)
    encoded_range = urllib.parse.quote(range_name)
    url = (
        f"https://sheets.googleapis.com/v4/spreadsheets/{SHIFT_SPREADSHEET_ID}"
        f"/values/{encoded_range}"
    )
    headers = {"Authorization": f"Bearer {token}"}
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req) as resp:
        data = json.loads(resp.read())
    return data.get("values", [])


def sheets_write(range_name, values):
    """Write values to Google Sheets."""
    cred_path = _find_google_credential()
    if not cred_path:
        raise RuntimeError("No Google OAuth credential found")

    token = _get_access_token(cred_path)
    encoded_range = urllib.parse.quote(range_name)
    url = (
        f"https://sheets.googleapis.com/v4/spreadsheets/{SHIFT_SPREADSHEET_ID}"
        f"/values/{encoded_range}?valueInputOption=USER_ENTERED"
    )
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    body = json.dumps({"range": range_name, "majorDimension": "ROWS", "values": values})
    req = urllib.request.Request(url, data=body.encode(), headers=headers, method="PUT")
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read())


def sheets_clear(range_name):
    """Clear a range in Google Sheets."""
    cred_path = _find_google_credential()
    if not cred_path:
        raise RuntimeError("No Google OAuth credential found")

    token = _get_access_token(cred_path)
    encoded_range = urllib.parse.quote(range_name)
    url = (
        f"https://sheets.googleapis.com/v4/spreadsheets/{SHIFT_SPREADSHEET_ID}"
        f"/values/{encoded_range}:clear"
    )
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    req = urllib.request.Request(url, data=b"{}", headers=headers, method="POST")
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read())


def sheets_batch_update(requests_body):
    """Send batchUpdate request for formatting etc."""
    cred_path = _find_google_credential()
    if not cred_path:
        raise RuntimeError("No Google OAuth credential found")

    token = _get_access_token(cred_path)
    url = (
        f"https://sheets.googleapis.com/v4/spreadsheets/{SHIFT_SPREADSHEET_ID}"
        f":batchUpdate"
    )
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    body = json.dumps({"requests": requests_body})
    req = urllib.request.Request(url, data=body.encode(), headers=headers, method="POST")
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read())


def _find_google_credential():
    """Find Google OAuth credential file for satoru@chillaxy.jp."""
    import os
    base_dir = os.path.expanduser("~/.google_workspace_mcp")
    for subdir in ["chillaxy", "gw-chillaxy"]:
        base = os.path.join(base_dir, subdir)
        target = os.path.join(base, "satoru@chillaxy.jp.json")
        if os.path.exists(target):
            return target
    if os.path.isdir(base_dir):
        for subdir in os.listdir(base_dir):
            path = os.path.join(base_dir, subdir, "satoru@chillaxy.jp.json")
            if os.path.exists(path):
                return path
    return None


def _get_access_token(cred_path):
    """Get access token from credential file, refreshing if needed."""
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
        from datetime import timezone
        new_expiry = (datetime.now(timezone.utc) + timedelta(seconds=expires_in)).isoformat()
        cred["expiry"] = new_expiry
        cred["token_expiry"] = new_expiry
        with open(cred_path, "w") as f:
            json.dump(cred, f, indent=2)

        return new_token

    if token:
        return token

    raise RuntimeError(f"Cannot get access token from {cred_path}")


def claude_api(prompt, system_prompt=""):
    """Call Anthropic Claude API."""
    if not ANTHROPIC_API_KEY:
        raise RuntimeError("ANTHROPIC_API_KEY not set")

    url = "https://api.anthropic.com/v1/messages"
    headers = {
        "Content-Type": "application/json",
        "x-api-key": ANTHROPIC_API_KEY,
        "anthropic-version": "2023-06-01",
    }
    body = {
        "model": "claude-sonnet-4-20250514",
        "max_tokens": 8192,
        "messages": [{"role": "user", "content": prompt}],
    }
    if system_prompt:
        body["system"] = system_prompt

    data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            result = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        error_body = e.read().decode()
        raise RuntimeError(f"Claude API error ({e.code}): {error_body}")

    for block in result.get("content", []):
        if block.get("type") == "text":
            return block["text"]
    return ""


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


def slack_notify_success(period_start, period_end, staff_count):
    if not SLACK_BOT_TOKEN:
        return
    text = (
        f"*シフト自動生成完了*\n"
        f"対象期間: {period_start} ~ {period_end}\n"
        f"スタッフ数: {staff_count}名\n"
        f"スプレッドシートの「シフト出力」シートを確認してください。"
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
# (1) Read master data from spreadsheet
# ---------------------------------------------------------------------------

def read_staff_master():
    """Read staff master from spreadsheet. Returns list of dicts keyed by staff_id.

    Reads A1:N100. Checkbox columns I-M (store assignments).
    対応店舗 is computed from store checkbox columns at read time.
    """
    rows = sheets_read("スタッフマスタ!A1:Q100")
    if not rows:
        raise RuntimeError("スタッフマスタが空です")

    header = rows[0]
    store_names = ["藤沢", "伊勢佐木町", "新宿", "工場", "本部オフィス", "EC"]
    staff = []
    for row in rows[1:]:
        if not row or not row[0]:
            continue
        row += [""] * (len(header) - len(row))
        d = {}
        for i, h in enumerate(header):
            d[h] = row[i]
        # Compute 対応店舗 from store checkbox columns
        d["対応店舗"] = ", ".join(
            name for name in store_names if str(d.get(name, "")).upper() == "TRUE"
        )
        if d.get("有効フラグ", "") == "無効":
            continue
        if not d.get(STAFF_ID_COLUMN):
            print(f"  WARNING: staff_id missing for {d.get('氏名', '?')}, skipping")
            continue
        staff.append(d)

    print(f"  Staff loaded: {len(staff)} active members")
    return staff


def read_rules_master():
    """Read 共通ルールマスタ as free-text lines. Returns List[str]."""
    rows = sheets_read("共通ルールマスタ!A3:A100")
    rules = []
    for row in rows:
        if row and row[0] and str(row[0]).strip():
            rules.append(str(row[0]).strip())
    print(f"  Rules loaded: {len(rules)} lines")
    return rules


def read_store_master():
    """Read 店舗マスタ. Returns list of dicts for active stores."""
    rows = sheets_read("店舗マスタ!A1:H100")
    if not rows:
        return []
    headers = rows[0]
    stores = []
    for row in rows[1:]:
        while len(row) < len(headers):
            row.append("")
        d = {headers[i]: row[i] for i in range(len(headers))}
        if d.get("有効フラグ", "有効") == "無効":
            continue
        stores.append(d)
    print(f"  Stores loaded: {len(stores)} active stores")
    return stores


# ---------------------------------------------------------------------------
# (2) Fetch wish records from kintone (per-day format)
# ---------------------------------------------------------------------------

def fetch_wishes(period_start, period_end):
    """Fetch shift wish records from kintone for the target period.

    Returns dict: {staff_id: {date_str: {shift_type, start_time, end_time, staff_name}}}
    """
    query = (
        f'target_period_start = "{period_start}" '
        f'and target_period_end = "{period_end}" '
        f'and input_status in ("入力済")'
    )
    encoded = urllib.parse.quote(query + " order by shift_date asc limit 500")
    result = kintone_api(
        f"records.json?app={KINTONE_SHIFT_WISH_APP_ID}&query={encoded}"
    )
    records = result.get("records", [])

    wishes = {}
    for r in records:
        sid = r.get("staff_id", {}).get("value", "")
        date = r.get("shift_date", {}).get("value", "")
        if not sid or not date:
            continue
        if sid not in wishes:
            wishes[sid] = {}
        wishes[sid][date] = {
            "staff_name": r.get("staff_name", {}).get("value", ""),
            "shift_type": r.get("shift_type", {}).get("value", "出勤"),
            "start_time": r.get("start_time", {}).get("value", ""),
            "end_time": r.get("end_time", {}).get("value", ""),
        }

    total = sum(len(v) for v in wishes.values())
    print(f"  Wishes loaded: {total} day-records for {len(wishes)} staff from kintone")
    return wishes


# ---------------------------------------------------------------------------
# (3) Write wish data to '希望収集データ' sheet (grid format)
# ---------------------------------------------------------------------------

def write_wishes_to_sheet(wishes, staff, dates):
    """Write wish data in grid format matching reference 希望シフト sheet.

    Layout: Row 1 = staff names, Row 2+ = [date_label, cell_per_staff]
    Cell values: "HH:MM-HH:MM" (work), "×" (off/希望休), "" (available/no data)
    """
    sheets_clear("希望収集データ!A1:AZ200")

    # Only show 希望シフト / 混合 staff (fixed shift staff don't submit wishes)
    wish_staff = [s for s in staff if s.get("働き方") in ("希望シフト", "混合")]

    if not wish_staff:
        print("  No 希望シフト staff to display")
        return

    # Row 1: header with staff names
    header = [""] + [s["氏名"] for s in wish_staff]
    rows = [header]

    # Data rows: one per date
    for date in dates:
        d = datetime.strptime(date, "%Y-%m-%d")
        date_label = f"{d.month}/{d.day}"
        row = [date_label]
        for s in wish_staff:
            sid = s[STAFF_ID_COLUMN]
            day = wishes.get(sid, {}).get(date, {})
            stype = day.get("shift_type", "")
            start = day.get("start_time", "")
            end = day.get("end_time", "")
            if stype in ("休み", "希望休"):
                row.append("×")
            elif start and end:
                row.append(f"{start}-{end}")
            else:
                row.append("")
        rows.append(row)

    end_col = col_letter(len(header) - 1)
    sheets_write(f"希望収集データ!A1:{end_col}{len(rows)}", rows)
    print(f"  Written wish grid: {len(dates)} dates x {len(wish_staff)} staff")


# ---------------------------------------------------------------------------
# (4) Call Claude API to generate shift (store-based)
# ---------------------------------------------------------------------------

def generate_dates(period_start, period_end):
    """Generate list of date strings for the period."""
    start = datetime.strptime(period_start, "%Y-%m-%d")
    end = datetime.strptime(period_end, "%Y-%m-%d")
    dates = []
    d = start
    while d <= end:
        dates.append(d.strftime("%Y-%m-%d"))
        d += timedelta(days=1)
    return dates


def day_label(date_str):
    """Return Japanese day-of-week label."""
    dow = ["月", "火", "水", "木", "金", "土", "日"]
    d = datetime.strptime(date_str, "%Y-%m-%d")
    return dow[d.weekday()]


def build_claude_prompt(staff, rules, stores, wishes, dates):
    """Build prompt for Claude to generate store-assigned shift schedule."""

    retail_stores = [s for s in stores if s.get("種別") == "営業"]
    other_stores = [s for s in stores if s.get("種別") != "営業"]

    # Staff info (役員は自動生成対象外のため除外)
    staff = [s for s in staff if s.get("雇用形態", "") != "役員"]
    staff_info = []
    for s in staff:
        name = s.get("氏名", "")
        emp_type = s.get("雇用形態", "")
        shift_type = s.get("働き方", "")
        store = s.get("対応店舗", "")
        position = s.get("役職", "")
        personal_rule = s.get("個人ルール", "")
        staff_info.append(
            f"- {name} (雇用:{emp_type}, 働き方:{shift_type}, 役職:{position}, "
            f"対応店舗:{store or '全店舗'}"
            f"{', ルール:' + personal_rule if personal_rule else ''})"
        )

    # Wish info per staff
    wish_lines = []
    for s in staff:
        sid = s[STAFF_ID_COLUMN]
        name = s["氏名"]
        staff_wishes = wishes.get(sid, {})
        if not staff_wishes:
            continue
        entries = []
        for date in dates:
            day = staff_wishes.get(date, {})
            stype = day.get("shift_type", "")
            start = day.get("start_time", "")
            end = day.get("end_time", "")
            d = datetime.strptime(date, "%Y-%m-%d")
            dl = f"{d.month}/{d.day}"
            if stype in ("休み", "希望休"):
                entries.append(f"{dl}:×")
            elif start and end:
                entries.append(f"{dl}:{start}-{end}")
        if entries:
            wish_lines.append(f"- {name}: {', '.join(entries)}")

    # Store info
    store_info = []
    for s in retail_stores:
        store_info.append(
            f"- {s['店舗名']}: 営業店舗、早番・遅番の2人体制、最低{s.get('最低必要人数/日', '2')}人/日"
        )
    for s in other_stores:
        store_info.append(f"- {s['店舗名']}: {s.get('種別', '')}")

    rules_text = "\n".join(f"- {r}" for r in rules) if rules else "なし"
    date_labels = [f"{d}({day_label(d)})" for d in dates]
    staff_names = [s["氏名"] for s in staff]
    retail_names = [s["店舗名"] for s in retail_stores]

    # Build example for one day
    example_stores = {}
    for rs in retail_stores:
        example_stores[rs["店舗名"]] = {"早番": "スタッフ名", "遅番": "スタッフ名"}
    for os_item in other_stores:
        example_stores[os_item["店舗名"]] = ["スタッフ名"]

    prompt = f"""以下の条件でシフトスケジュールを生成してください。
各スタッフを店舗に割り当て、勤務時間を決定してください。

## 対象期間
{dates[0]} ~ {dates[-1]} ({len(dates)}日間)

## 日付一覧
{', '.join(date_labels)}

## スタッフ一覧 ({len(staff)}名)
{chr(10).join(staff_info)}

## 店舗情報
{chr(10).join(store_info)}

## 希望シフトデータ
{chr(10).join(wish_lines) if wish_lines else '希望データなし'}

## 共通ルール
{rules_text}

## 店舗の早番・遅番について
- 営業店舗（{', '.join(retail_names)}）は早番・遅番の2交代制
- 早番: 概ね9:30〜16:00（開店準備〜午後）
- 遅番: 概ね15:30〜0:30（午後〜閉店）
- 各店舗に毎日必ず早番1名・遅番1名を配置
- 同一人物が通し勤務する場合あり（例: 11:30-0:30）

## 制約
1. 役員を除く全スタッフ（正社員の固定シフト・アルバイトの希望シフト）を配置対象とする
2. 正社員（固定シフト）は週40時間（週5日）が基本。最低人数が埋まらない日がある場合のみ残業可。ただし月の残業累計は45時間以内
3. 希望休（×マーク）は必ず尊重する
4. 対応店舗が指定されているスタッフはその店舗のみに配置
5. 最低週1日の休みを確保
6. 土日祝日も含め全日全店舗カバー

## 出力形式（JSONのみ、説明文不要）

```json
{{
  "schedule": [
    {{
      "date": "{dates[0]}",
      "stores": {{
        "{retail_stores[0]['店舗名'] if retail_stores else '藤沢'}": {{"早番": "スタッフ名", "遅番": "スタッフ名"}},
        "{retail_stores[1]['店舗名'] if len(retail_stores) > 1 else '伊勢佐木町'}": {{"早番": "スタッフ名", "遅番": "スタッフ名"}},
        "{retail_stores[2]['店舗名'] if len(retail_stores) > 2 else '新宿'}": {{"早番": "スタッフ名", "遅番": "スタッフ名"}},
        "{other_stores[0]['店舗名'] if other_stores else '工場'}": ["スタッフ名"]
      }},
      "times": {{
        "スタッフ名1": "11:30-16:00",
        "スタッフ名2": "15:30-0:30"
      }}
    }}
  ],
  "staff_groups": {{
    "店舗名": ["スタッフ名1", "スタッフ名2"]
  }},
  "notes": "補足"
}}
```

### JSON出力ルール:
- `schedule`: 全{len(dates)}日分を配列で出力
- `stores`: 営業店舗は{{"早番":"名前","遅番":"名前"}}、非営業は["名前"]
- `times`: その日に出勤するスタッフのみ（休みスタッフは含めない）
- `staff_groups`: 各スタッフの主要勤務店舗でグルーピング（表示用）。全スタッフを含めること
- スタッフ名は完全一致で使用: {json.dumps(staff_names, ensure_ascii=False)}
- 時間は "HH:MM-HH:MM" 形式（例: "9:30-16:00", "15:30-0:30"）
"""
    return prompt


MAX_GENERATION_RETRIES = 3


def _parse_claude_response(response):
    """Parse Claude API JSON response (store-based format)."""
    json_match = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", response, re.DOTALL)
    if json_match:
        json_str = json_match.group(1)
    else:
        json_str = response.strip()

    try:
        result = json.loads(json_str)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"Claude API response is not valid JSON: {e}\nResponse: {response[:500]}")

    schedule = result.get("schedule")
    if not schedule:
        raise RuntimeError(f"Empty schedule in response: {response[:500]}")

    return result


def _convert_to_legacy_schedule(result, staff, dates):
    """Convert store-based format to legacy {name: [status,...]} for validator."""
    staff_names = {s["氏名"] for s in staff}
    schedule = {name: [] for name in staff_names}

    for day_data in result["schedule"]:
        working = set(day_data.get("times", {}).keys())
        for name in staff_names:
            schedule[name].append("出勤" if name in working else "休み")

    return schedule


def _convert_wishes_for_validator(wishes, staff, dates):
    """Convert per-day wishes dict to flat list for validator."""
    flat = []
    for s in staff:
        sid = s[STAFF_ID_COLUMN]
        name = s["氏名"]
        staff_wishes = wishes.get(sid, {})
        off_dates = []
        for date in dates:
            day = staff_wishes.get(date, {})
            if day.get("shift_type") in ("休み", "希望休"):
                off_dates.append(date)
        if off_dates:
            flat.append({
                "staff_id": sid,
                "staff_name": name,
                "desired_days_off": ", ".join(off_dates),
                "remarks": "",
                "shift_date": "",
            })
    return flat


def call_claude_for_shift(staff, rules, stores, wishes, dates):
    """Call Claude API to generate shift schedule with validation and retry."""
    from shift_validator import validate_schedule, format_violations, has_hard_violations

    system = (
        "あなたはシフト管理の専門家です。与えられた条件を厳密に守り、"
        "最適なシフトスケジュールをJSON形式で生成してください。"
        "JSONのみを出力し、それ以外のテキストは一切含めないでください。"
    )

    last_violations = None
    wishes_flat = _convert_wishes_for_validator(wishes, staff, dates)

    for attempt in range(1, MAX_GENERATION_RETRIES + 1):
        prompt = build_claude_prompt(staff, rules, stores, wishes, dates)

        if last_violations:
            violation_text = format_violations(last_violations)
            prompt += (
                f"\n\n## 前回の生成結果にルール違反がありました。修正してください:\n"
                f"{violation_text}\n"
            )

        print(f"  Calling Claude API (attempt {attempt}/{MAX_GENERATION_RETRIES})...")
        response = claude_api(prompt, system)
        result = _parse_claude_response(response)

        schedule_days = result.get("schedule", [])
        print(f"  Schedule generated: {len(schedule_days)} days")
        if result.get("notes"):
            print(f"  Notes: {result['notes']}")

        # Convert to legacy format for validation
        legacy_schedule = _convert_to_legacy_schedule(result, staff, dates)
        violations = validate_schedule(legacy_schedule, staff, rules, dates, wishes_flat)
        print(f"  Validation: {format_violations(violations)}")

        if not has_hard_violations(violations):
            result["_violations"] = violations
            return result

        last_violations = violations
        print(f"  Hard violations found, {'retrying...' if attempt < MAX_GENERATION_RETRIES else 'giving up.'}")

    result["_violations"] = last_violations
    result["_needs_human_review"] = True
    print("  WARNING: Hard violations remain after all retries. Flagging for human review.")
    return result


# ---------------------------------------------------------------------------
# (5) Write results to 'シフト出力' sheet (store-based layout)
# ---------------------------------------------------------------------------

def _build_column_layout(stores, staff_groups):
    """Build column layout matching reference spreadsheet format.

    Returns:
        columns: list of dicts describing each column
        store_headers: list for the store header row
        sub_headers: list for the sub-header row
    """
    retail_stores = [s for s in stores if s.get("種別") == "営業"]
    other_stores = [s for s in stores if s.get("種別") != "営業"]

    columns = []
    store_headers = []
    sub_headers = []

    # System columns (A-D): 確定, 変更, date, day
    for key in ["confirmed", "changed", "date", "day"]:
        columns.append({"type": "system", "key": key})
    store_headers.extend(["", "", "", ""])
    sub_headers.extend(["", "", "", ""])

    # Store assignment section: retail stores have 早番/遅番, others have single col
    for store in retail_stores:
        name = store["店舗名"]
        columns.append({"type": "store_early", "store": name})
        columns.append({"type": "store_late", "store": name})
        columns.append({"type": "spacer"})
        store_headers.extend([name, "", ""])
        sub_headers.extend(["早番", "遅番", ""])

    for store in other_stores:
        name = store["店舗名"]
        columns.append({"type": "store_single", "store": name})
        columns.append({"type": "spacer"})
        store_headers.extend([name, ""])
        sub_headers.extend(["", ""])

    # Individual time section: grouped by store
    all_stores_ordered = retail_stores + other_stores
    for store in all_stores_ordered:
        name = store["店舗名"]
        group = staff_groups.get(name, [])
        if not group:
            continue
        first = True
        for staff_name in group:
            columns.append({"type": "individual", "store": name, "staff": staff_name})
            store_headers.append(name if first else "")
            sub_headers.append(staff_name)
            first = False
        columns.append({"type": "spacer"})
        store_headers.append("")
        sub_headers.append("")

    return columns, store_headers, sub_headers


def write_shift_output(schedule_result, staff, stores, dates, period_start, period_end):
    """Write shift schedule to 'シフト出力' sheet in store-based format."""
    schedule = schedule_result["schedule"]
    staff_groups = schedule_result.get("staff_groups", {})

    # Fallback: if no staff_groups, put all staff under first store
    if not staff_groups:
        all_names = [s["氏名"] for s in staff]
        if stores:
            staff_groups = {stores[0]["店舗名"]: all_names}
        else:
            staff_groups = {"スタッフ": all_names}

    # Build column layout
    columns, store_headers, sub_headers = _build_column_layout(stores, staff_groups)

    # Clear sheet
    end_col_letter = col_letter(max(len(columns) - 1, 25))
    sheets_clear(f"シフト出力!A1:{end_col_letter}200")

    all_rows = []

    # Row 1: metadata + year/month
    meta_pad = [""] * max(0, len(columns) - 6)
    year = period_start.split("-")[0]
    month = str(int(period_start.split("-")[1]))
    all_rows.append(["シフト期間", f"{period_start} ~ {period_end}", year, "年", month, "月"] + meta_pad)

    # Row 2: store headers
    all_rows.append(store_headers)

    # Row 3: sub-headers (早番/遅番 + staff names)
    all_rows.append(sub_headers)

    # Compute rest day counts per staff (for label row)
    staff_rest_counts = {}
    for day_data in schedule:
        working = set(day_data.get("times", {}).keys())
        for sg_staff_list in staff_groups.values():
            for sname in sg_staff_list:
                if sname not in staff_rest_counts:
                    staff_rest_counts[sname] = 0
                if sname not in working:
                    staff_rest_counts[sname] += 1

    # Row 4: column labels (確定/変更/blank/休日) + rest day counts
    label_row = [""] * len(columns)
    label_row[0] = "確定"
    label_row[1] = "変更"
    label_row[3] = "休日"
    for i, col in enumerate(columns):
        if col["type"] == "individual":
            staff_name = col["staff"]
            rest_count = staff_rest_counts.get(staff_name, 0)
            label_row[i] = str(rest_count) if rest_count else ""
    all_rows.append(label_row)

    for day_data in schedule:
        date_str = day_data["date"]
        d = datetime.strptime(date_str, "%Y-%m-%d")
        date_label = f"{d.month}/{d.day}"
        dow = day_label(date_str)

        store_assignments = day_data.get("stores", {})
        times = day_data.get("times", {})

        row = []
        for col in columns:
            ctype = col["type"]
            if ctype == "system":
                if col["key"] == "confirmed":
                    row.append("FALSE")
                elif col["key"] == "changed":
                    row.append("FALSE")
                elif col["key"] == "date":
                    row.append(date_label)
                elif col["key"] == "day":
                    row.append(dow)
            elif ctype == "store_early":
                assignments = store_assignments.get(col["store"], {})
                if isinstance(assignments, dict):
                    row.append(assignments.get("早番", ""))
                else:
                    row.append(assignments[0] if assignments else "")
            elif ctype == "store_late":
                assignments = store_assignments.get(col["store"], {})
                if isinstance(assignments, dict):
                    row.append(assignments.get("遅番", ""))
                else:
                    row.append(assignments[1] if len(assignments) > 1 else "")
            elif ctype == "store_single":
                assignments = store_assignments.get(col["store"], [])
                if isinstance(assignments, list):
                    row.append(", ".join(assignments))
                elif isinstance(assignments, dict):
                    vals = [v for v in assignments.values() if v]
                    row.append(", ".join(vals))
                else:
                    row.append(str(assignments))
            elif ctype == "individual":
                row.append(times.get(col["staff"], ""))
            elif ctype == "spacer":
                row.append("")

        all_rows.append(row)

    # Write all data
    end_col = col_letter(len(columns) - 1)
    end_row = len(all_rows)
    sheets_write(f"シフト出力!A1:{end_col}{end_row}", all_rows)
    print(f"  Written shift output: {len(dates)} days, {len(columns)} columns")

    return end_row, len(columns)


def apply_formatting(stores, staff_groups, end_row, num_columns):
    """Apply formatting to the shift output sheet."""
    columns, _, _ = _build_column_layout(stores, staff_groups)

    format_requests = []

    # Delete existing conditional format rules
    try:
        for _ in range(10):
            sheets_batch_update([{
                "deleteConditionalFormatRule": {
                    "sheetId": SHEET_ID_OUTPUT,
                    "index": 0,
                }
            }])
    except Exception:
        pass

    # Bold + center for store header row (row 2, index 1)
    format_requests.append({
        "repeatCell": {
            "range": {
                "sheetId": SHEET_ID_OUTPUT,
                "startRowIndex": 1,
                "endRowIndex": 2,
                "startColumnIndex": 0,
                "endColumnIndex": num_columns,
            },
            "cell": {
                "userEnteredFormat": {
                    "textFormat": {"bold": True, "fontSize": 11},
                    "horizontalAlignment": "CENTER",
                }
            },
            "fields": "userEnteredFormat(textFormat,horizontalAlignment)",
        }
    })

    # Bold + center for sub-header row (row 3, index 2) and label row (row 4, index 3)
    format_requests.append({
        "repeatCell": {
            "range": {
                "sheetId": SHEET_ID_OUTPUT,
                "startRowIndex": 2,
                "endRowIndex": 4,
                "startColumnIndex": 0,
                "endColumnIndex": num_columns,
            },
            "cell": {
                "userEnteredFormat": {
                    "textFormat": {"bold": True},
                    "horizontalAlignment": "CENTER",
                }
            },
            "fields": "userEnteredFormat(textFormat,horizontalAlignment)",
        }
    })

    # Title row bold + larger font
    format_requests.append({
        "repeatCell": {
            "range": {
                "sheetId": SHEET_ID_OUTPUT,
                "startRowIndex": 0,
                "endRowIndex": 1,
                "startColumnIndex": 0,
                "endColumnIndex": 2,
            },
            "cell": {
                "userEnteredFormat": {
                    "textFormat": {"bold": True, "fontSize": 12},
                }
            },
            "fields": "userEnteredFormat(textFormat)",
        }
    })

    # Center-align data area (row 5+, all columns)
    format_requests.append({
        "repeatCell": {
            "range": {
                "sheetId": SHEET_ID_OUTPUT,
                "startRowIndex": 4,
                "endRowIndex": end_row,
                "startColumnIndex": 0,
                "endColumnIndex": num_columns,
            },
            "cell": {
                "userEnteredFormat": {
                    "horizontalAlignment": "CENTER",
                }
            },
            "fields": "userEnteredFormat(horizontalAlignment)",
        }
    })

    # Light blue background for store assignment section headers (row 2-3)
    store_col_start = 4  # After system cols A-D
    store_col_end = store_col_start
    for col in columns[4:]:
        if col["type"] in ("store_early", "store_late", "store_single", "spacer"):
            store_col_end += 1
        else:
            break

    if store_col_end > store_col_start:
        format_requests.append({
            "repeatCell": {
                "range": {
                    "sheetId": SHEET_ID_OUTPUT,
                    "startRowIndex": 1,
                    "endRowIndex": 3,
                    "startColumnIndex": store_col_start,
                    "endColumnIndex": store_col_end,
                },
                "cell": {
                    "userEnteredFormat": {
                        "backgroundColor": {
                            "red": 0.85, "green": 0.92, "blue": 1.0, "alpha": 1,
                        },
                    }
                },
                "fields": "userEnteredFormat(backgroundColor)",
            }
        })

    # Light yellow background for individual time section headers (row 2-3)
    indiv_col_start = store_col_end
    if indiv_col_start < num_columns:
        format_requests.append({
            "repeatCell": {
                "range": {
                    "sheetId": SHEET_ID_OUTPUT,
                    "startRowIndex": 1,
                    "endRowIndex": 3,
                    "startColumnIndex": indiv_col_start,
                    "endColumnIndex": num_columns,
                },
                "cell": {
                    "userEnteredFormat": {
                        "backgroundColor": {
                            "red": 1.0, "green": 0.97, "blue": 0.85, "alpha": 1,
                        },
                    }
                },
                "fields": "userEnteredFormat(backgroundColor)",
            }
        })

    # Auto-resize columns
    format_requests.append({
        "autoResizeDimensions": {
            "dimensions": {
                "sheetId": SHEET_ID_OUTPUT,
                "dimension": "COLUMNS",
                "startIndex": 0,
                "endIndex": num_columns,
            }
        }
    })

    sheets_batch_update(format_requests)
    print("  Formatting applied (headers, alignment, colors)")


# ---------------------------------------------------------------------------
# Main flow
# ---------------------------------------------------------------------------

def get_next_shift_period():
    """Calculate next 2-week shift period."""
    today = datetime.now()
    days_ahead = 7 - today.weekday()
    if days_ahead <= 0:
        days_ahead += 7
    start = today + timedelta(days=days_ahead)
    end = start + timedelta(days=13)
    return start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")


def run(period_start=None, period_end=None):
    """Main entry point for shift generation."""
    if not period_start or not period_end:
        period_start, period_end = get_next_shift_period()

    print(f"=== Shift Generation: {period_start} ~ {period_end} ===\n")

    try:
        # (1) Read master data
        print("[1/5] Reading master data from spreadsheet...")
        staff = read_staff_master()
        rules = read_rules_master()
        stores = read_store_master()

        if not staff:
            raise RuntimeError("No active staff found in スタッフマスタ")

        # (2) Fetch wishes from kintone
        print("[2/5] Fetching wish records from kintone...")
        wishes = fetch_wishes(period_start, period_end)
        dates = generate_dates(period_start, period_end)

        # (3) Write wishes to sheet (grid format)
        print("[3/5] Writing wishes to '希望収集データ' sheet...")
        write_wishes_to_sheet(wishes, staff, dates)

        # (4) Generate shift with Claude API
        print("[4/5] Generating shift schedule with Claude API...")
        schedule_result = call_claude_for_shift(staff, rules, stores, wishes, dates)

        # (5) Write to output sheet (store-based format)
        print("[5/5] Writing results to 'シフト出力' sheet...")
        staff_groups = schedule_result.get("staff_groups", {})
        end_row, num_cols = write_shift_output(
            schedule_result, staff, stores, dates, period_start, period_end
        )
        apply_formatting(stores, staff_groups, end_row, num_cols)

        if schedule_result.get("_needs_human_review"):
            print(f"\n=== Shift generation complete (NEEDS HUMAN REVIEW) ===")
            slack_notify_error("generate_shift",
                "シフト生成完了。ハードルール違反が残っています。手動確認が必要です。")
        else:
            print(f"\n=== Shift generation complete ===")
            slack_notify_success(period_start, period_end, len(staff))

    except Exception as e:
        error_msg = str(e)
        print(f"\nERROR: {error_msg}")
        slack_notify_error("generate_shift", error_msg)
        raise


def main():
    if len(sys.argv) >= 3:
        run(sys.argv[1], sys.argv[2])
    elif len(sys.argv) == 2 and sys.argv[1] == "--help":
        print(__doc__)
    else:
        run()


if __name__ == "__main__":
    main()
