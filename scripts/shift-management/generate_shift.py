#!/usr/bin/env python3
"""STEP 6: AI shift auto-generation.

Processing order:
  (1) Read staff master & rule master from spreadsheet
  (2) Fetch wish records from kintone app 211 for target period
  (3) Write wish data to '希望収集データ' sheet
  (4) Call Claude API to generate shift schedule
  (5) Write results to 'シフト出力' sheet with conditional formatting

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
    """Read from Google Sheets via gw-chillaxy MCP proxy is not available
    in standalone scripts. Use Sheets API v4 with service account or
    OAuth token from cached credentials."""
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
    # Try known subdirectory names
    for subdir in ["chillaxy", "gw-chillaxy"]:
        base = os.path.join(base_dir, subdir)
        target = os.path.join(base, "satoru@chillaxy.jp.json")
        if os.path.exists(target):
            return target
    # Fallback: search all subdirs
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

    # Check if token is still valid
    token = cred.get("token") or cred.get("access_token")
    expiry = cred.get("expiry") or cred.get("token_expiry")

    if token and expiry:
        try:
            exp_dt = datetime.fromisoformat(expiry.replace("Z", "+00:00"))
            if exp_dt > datetime.now(exp_dt.tzinfo):
                return token
        except (ValueError, TypeError):
            pass

    # Try to refresh
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

        # Update cached credential
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


def claude_api(prompt, system_prompt=""):
    """Call Anthropic Claude API.

    Tries direct API first. If ANTHROPIC_API_KEY is not set or credits
    are insufficient, raises RuntimeError so caller can handle it.
    """
    if not ANTHROPIC_API_KEY:
        raise RuntimeError("ANTHROPIC_API_KEY not set")

    full_prompt = prompt
    if system_prompt:
        full_prompt = f"[System]: {system_prompt}\n\n{prompt}"

    url = "https://api.anthropic.com/v1/messages"
    headers = {
        "Content-Type": "application/json",
        "x-api-key": ANTHROPIC_API_KEY,
        "anthropic-version": "2023-06-01",
    }
    body = {
        "model": "claude-sonnet-4-20250514",
        "max_tokens": 4096,
        "messages": [{"role": "user", "content": full_prompt}],
    }
    if system_prompt:
        body["system"] = system_prompt

    data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            result = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        error_body = e.read().decode()
        raise RuntimeError(f"Claude API error ({e.code}): {error_body}")

    # Extract text from response
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
    """Read staff master from spreadsheet. Returns list of dicts keyed by staff_id."""
    rows = sheets_read("スタッフマスタ!A1:I100")
    if not rows:
        raise RuntimeError("スタッフマスタが空です")

    header = rows[0]
    staff = []
    for row in rows[1:]:
        if not row or not row[0]:
            continue
        # Pad row to header length
        row += [""] * (len(header) - len(row))
        d = {}
        for i, h in enumerate(header):
            d[h] = row[i]
        # Only include active staff
        if d.get("有効フラグ", "").upper() in ("FALSE", "0", "無効", ""):
            if d.get("有効フラグ", "") == "":
                # Empty = default active
                pass
            else:
                continue
        # Require staff_id
        if not d.get(STAFF_ID_COLUMN):
            print(f"  WARNING: staff_id missing for {d.get('氏名', '?')}, skipping")
            continue
        staff.append(d)

    print(f"  Staff loaded: {len(staff)} active members")
    return staff


def read_rule_master():
    """Read rule master from spreadsheet. Returns dict of rule_name -> value."""
    rows = sheets_read("ルールマスタ!A1:C20")
    if not rows:
        raise RuntimeError("ルールマスタが空です")

    rules = {}
    for row in rows[1:]:
        if len(row) >= 2 and row[0]:
            rules[row[0]] = row[1]

    print(f"  Rules loaded: {rules}")
    return rules


# ---------------------------------------------------------------------------
# (2) Fetch wish records from kintone
# ---------------------------------------------------------------------------

def fetch_wishes(period_start, period_end):
    """Fetch shift wish records from kintone for the target period."""
    query = (
        f'target_period_start = "{period_start}" '
        f'and target_period_end = "{period_end}"'
    )
    result = kintone_api(
        f"records.json?app={KINTONE_SHIFT_WISH_APP_ID}"
        f"&query={urllib.parse.quote(query)}&totalCount=true"
    )
    records = result.get("records", [])
    wishes = []
    for r in records:
        wishes.append({
            "staff_id": r.get("staff_id", {}).get("value", ""),
            "staff_name": r["staff_name"]["value"],
            "desired_days_off": r["desired_days_off"]["value"],
            "remarks": r["remarks"]["value"],
            "input_channel": r["input_channel"]["value"],
            "submitted_at": r["submitted_at"]["value"],
            "status": r["status"]["value"],
        })

    print(f"  Wishes loaded: {len(wishes)} records from kintone")
    return wishes


# ---------------------------------------------------------------------------
# (3) Write wish data to '希望収集データ' sheet
# ---------------------------------------------------------------------------

def write_wishes_to_sheet(wishes):
    """Write wish data to the '希望収集データ' sheet."""
    # Clear existing data (keep header)
    sheets_clear("希望収集データ!A2:H1000")

    if not wishes:
        print("  No wishes to write")
        return

    rows = []
    for w in wishes:
        rows.append([
            w["staff_name"],
            "",  # period start (in kintone record)
            "",  # period end
            w["desired_days_off"],
            w["remarks"],
            w["input_channel"],
            w["submitted_at"],
            w["status"],
        ])

    sheets_write(f"希望収集データ!A2:H{1 + len(rows)}", rows)
    print(f"  Written {len(rows)} wish records to sheet")


# ---------------------------------------------------------------------------
# (4) Call Claude API to generate shift
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


def build_claude_prompt(staff, rules, wishes, dates):
    """Build the prompt for Claude to generate shift schedule."""

    # Build staff info
    staff_info = []
    for s in staff:
        name = s.get("氏名", "")
        emp_type = s.get("雇用形態", "")
        max_hours = s.get("最大労働時間/週", "40")
        forced = s.get("強制出勤日", "")
        skills = s.get("役割・スキル", "")
        staff_info.append(
            f"- {name} (雇用: {emp_type}, 週最大: {max_hours}h, "
            f"強制出勤日: {forced or 'なし'}, スキル: {skills or 'なし'})"
        )

    # Build staff_id -> name map for wish matching
    name_to_id = {s.get("氏名", ""): s.get(STAFF_ID_COLUMN, "") for s in staff}

    # Build wish info
    wish_info = []
    for w in wishes:
        wish_info.append(
            f"- {w['staff_name']}: 希望休={w['desired_days_off']}"
            f"{' (備考: ' + w['remarks'] + ')' if w['remarks'] else ''}"
        )

    # Rules
    min_staff = rules.get("最低必須人数", "3")
    max_consecutive = rules.get("連続勤務上限日数", "5")
    wish_priority = rules.get("希望休優先度（重み）", "7")

    # Date list with day-of-week
    date_labels = [f"{d}({day_label(d)})" for d in dates]

    staff_names = [s.get("氏名", "") for s in staff]

    prompt = f"""以下の条件でシフトスケジュールを生成してください。

## 対象期間
{dates[0]} ~ {dates[-1]} ({len(dates)}日間)

## 日付一覧
{', '.join(date_labels)}

## スタッフ一覧
{chr(10).join(staff_info)}

## 希望休データ
{chr(10).join(wish_info) if wish_info else '希望休なし'}

## ルール（必ず守ること）
1. 強制出勤日が指定されたスタッフは、その曜日に必ず「出勤」にする
2. 毎日の出勤人数が最低{min_staff}人以上になること
3. 連続勤務日数は最大{max_consecutive}日まで（超えたら休みを入れる）
4. 希望休をできるだけ反映する（優先度: {wish_priority}/10）
5. 各スタッフの最大労働時間/週（8h/日で計算）を超えないこと
6. 土日祝日も含め全日カバーすること

## 出力形式
以下のJSON形式のみで回答してください。説明文は不要です。

```json
{{
  "schedule": {{
    "スタッフ名": [
      "出勤 or 休み or 希望休",  // {dates[0]}
      ...  // 全{len(dates)}日分
    ],
    ...  // 全スタッフ分
  }},
  "daily_count": [人数, ...],  // 各日の出勤人数
  "notes": "生成時の補足（ルール違反があれば記載）"
}}
```

スタッフ名は以下の順序で出力: {json.dumps(staff_names, ensure_ascii=False)}
各スタッフの配列は必ず{len(dates)}要素にしてください。
値は「出勤」「休み」「希望休」の3種類のみです。
"""
    return prompt


MAX_GENERATION_RETRIES = 3


def _parse_claude_response(response):
    """Parse Claude API JSON response."""
    json_match = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", response, re.DOTALL)
    if json_match:
        json_str = json_match.group(1)
    else:
        json_str = response.strip()

    try:
        result = json.loads(json_str)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"Claude API response is not valid JSON: {e}\nResponse: {response[:500]}")

    schedule = result.get("schedule", {})
    if not schedule:
        raise RuntimeError(f"Claude API returned empty schedule: {response[:500]}")

    return result


def call_claude_for_shift(staff, rules, wishes, dates):
    """Call Claude API to generate shift schedule with validation and retry.

    LLM generates candidate schedule, Python validator checks hard rules.
    Retries up to MAX_GENERATION_RETRIES times on hard violations.
    """
    from shift_validator import validate_schedule, format_violations, has_hard_violations

    system = (
        "あなたはシフト管理の専門家です。与えられた条件を厳密に守り、"
        "最適なシフトスケジュールをJSON形式で生成してください。"
        "JSONのみを出力し、それ以外のテキストは一切含めないでください。"
    )

    last_violations = None

    for attempt in range(1, MAX_GENERATION_RETRIES + 1):
        prompt = build_claude_prompt(staff, rules, wishes, dates)

        # On retry, append violation feedback to prompt
        if last_violations:
            violation_text = format_violations(last_violations)
            prompt += (
                f"\n\n## 前回の生成結果にルール違反がありました。修正してください:\n"
                f"{violation_text}\n"
            )

        print(f"  Calling Claude API (attempt {attempt}/{MAX_GENERATION_RETRIES})...")
        response = claude_api(prompt, system)
        result = _parse_claude_response(response)

        print(f"  Schedule generated for {len(result['schedule'])} staff")
        if result.get("notes"):
            print(f"  Notes: {result['notes']}")

        # Validate
        violations = validate_schedule(result["schedule"], staff, rules, dates, wishes)
        print(f"  Validation: {format_violations(violations)}")

        if not has_hard_violations(violations):
            result["_violations"] = violations  # keep soft warnings
            return result

        last_violations = violations
        print(f"  Hard violations found, {'retrying...' if attempt < MAX_GENERATION_RETRIES else 'giving up.'}")

    # All retries exhausted - flag for human review
    result["_violations"] = last_violations
    result["_needs_human_review"] = True
    print("  WARNING: Hard violations remain after all retries. Flagging for human review.")
    return result


# ---------------------------------------------------------------------------
# (5) Write results to 'シフト出力' sheet
# ---------------------------------------------------------------------------

def write_shift_output(schedule_result, staff, dates, period_start, period_end):
    """Write shift schedule to 'シフト出力' sheet."""
    schedule = schedule_result["schedule"]

    # Clear entire sheet
    sheets_clear("シフト出力!A1:S100")

    # Build rows
    all_rows = []

    # Row 1: title
    all_rows.append(["シフト期間", f"{period_start} ~ {period_end}"])

    # Row 2-3: approval placeholders (filled by GAS on approval)
    all_rows.append(["承認者", ""])
    all_rows.append(["承認日時", ""])

    # Row 4: schedule_version placeholder (filled by GAS on approval)
    all_rows.append(["schedule_version", ""])

    # Row 5: date headers (A5=staff_id, B5=スタッフ名, C5~P5=dates, Q5=出勤日数, R5=合計勤務時間)
    date_headers = [f"{d}\n({day_label(d)})" for d in dates]
    header_row = [STAFF_ID_COLUMN, "スタッフ名"] + date_headers + ["出勤日数", "合計勤務時間"]
    all_rows.append(header_row)

    # Row 6+: staff data
    for s in staff:
        staff_id = s.get(STAFF_ID_COLUMN, "")
        name = s.get("氏名", "")
        days = schedule.get(name, ["休み"] * len(dates))
        # Ensure correct length
        days = (days + ["休み"] * len(dates))[:len(dates)]
        work_days = sum(1 for d in days if d == "出勤")
        work_hours = work_days * 8
        row = [staff_id, name] + days + [str(work_days), f"{work_hours}h"]
        all_rows.append(row)

    # Write all data
    end_row = 5 + len(staff)
    end_col = chr(ord("A") + len(dates) + 3)  # +3 for staff_id, name, counts
    sheets_write(f"シフト出力!A1:{end_col}{end_row}", all_rows)
    print(f"  Written shift output: {len(staff)} staff x {len(dates)} days")

    return end_row


def apply_conditional_formatting(dates, end_row):
    """Apply conditional formatting to the shift output sheet.

    出勤=green, 休み=white, 希望休=yellow
    """
    num_dates = len(dates)
    # Data range: C6 to P{end_row} (columns 2-15, rows 5-end_row, 0-indexed)
    data_range = {
        "sheetId": SHEET_ID_OUTPUT,
        "startRowIndex": 5,       # row 6 (0-indexed)
        "endRowIndex": end_row,   # exclusive
        "startColumnIndex": 2,    # column C (shifted by staff_id col)
        "endColumnIndex": 2 + num_dates,  # column P+1
    }

    # Delete existing conditional format rules on this sheet first
    requests = [
        {
            "deleteConditionalFormatRule": {
                "sheetId": SHEET_ID_OUTPUT,
                "index": 0,
            }
        }
    ]
    # Try to delete up to 10 existing rules (ignore errors)
    try:
        for _ in range(10):
            sheets_batch_update(requests)
    except Exception:
        pass

    # Add new rules
    format_requests = [
        # 出勤 -> green
        {
            "addConditionalFormatRule": {
                "rule": {
                    "ranges": [data_range],
                    "booleanRule": {
                        "condition": {
                            "type": "TEXT_EQ",
                            "values": [{"userEnteredValue": "出勤"}],
                        },
                        "format": {
                            "backgroundColor": {
                                "red": 0.71, "green": 0.88, "blue": 0.70, "alpha": 1,
                            },
                        },
                    },
                },
                "index": 0,
            }
        },
        # 希望休 -> yellow
        {
            "addConditionalFormatRule": {
                "rule": {
                    "ranges": [data_range],
                    "booleanRule": {
                        "condition": {
                            "type": "TEXT_EQ",
                            "values": [{"userEnteredValue": "希望休"}],
                        },
                        "format": {
                            "backgroundColor": {
                                "red": 1.0, "green": 0.95, "blue": 0.60, "alpha": 1,
                            },
                        },
                    },
                },
                "index": 1,
            }
        },
        # 休み -> white
        {
            "addConditionalFormatRule": {
                "rule": {
                    "ranges": [data_range],
                    "booleanRule": {
                        "condition": {
                            "type": "TEXT_EQ",
                            "values": [{"userEnteredValue": "休み"}],
                        },
                        "format": {
                            "backgroundColor": {
                                "red": 1.0, "green": 1.0, "blue": 1.0, "alpha": 1,
                            },
                        },
                    },
                },
                "index": 2,
            }
        },
    ]

    # Also format header row (row 5) bold + center
    format_requests.append({
        "repeatCell": {
            "range": {
                "sheetId": SHEET_ID_OUTPUT,
                "startRowIndex": 4,
                "endRowIndex": 5,
                "startColumnIndex": 0,
                "endColumnIndex": 2 + len(generate_dates("2099-01-01", "2099-01-01")) + 16,
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

    # Format title row bold
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

    # Center-align data cells
    format_requests.append({
        "repeatCell": {
            "range": data_range,
            "cell": {
                "userEnteredFormat": {
                    "horizontalAlignment": "CENTER",
                }
            },
            "fields": "userEnteredFormat(horizontalAlignment)",
        }
    })

    sheets_batch_update(format_requests)
    print("  Conditional formatting applied (出勤:green, 休み:white, 希望休:yellow)")


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
        rules = read_rule_master()

        if not staff:
            raise RuntimeError("No active staff found in スタッフマスタ")

        # (2) Fetch wishes from kintone
        print("[2/5] Fetching wish records from kintone...")
        wishes = fetch_wishes(period_start, period_end)

        # (3) Write wishes to sheet
        print("[3/5] Writing wishes to '希望収集データ' sheet...")
        write_wishes_to_sheet(wishes)

        # (4) Generate shift with Claude API
        print("[4/5] Generating shift schedule with Claude API...")
        dates = generate_dates(period_start, period_end)
        schedule_result = call_claude_for_shift(staff, rules, wishes, dates)

        # (5) Write to output sheet
        print("[5/5] Writing results to 'シフト出力' sheet...")
        end_row = write_shift_output(schedule_result, staff, dates, period_start, period_end)
        apply_conditional_formatting(dates, end_row)

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
