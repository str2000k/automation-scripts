#!/usr/bin/env python3
"""Migrate legacy shift data from old spreadsheet to new system (kintone + spreadsheet).

Source: 14g75SYgDPTtXXLq8CgGqAYtmp9hCP_jMHS-vOO_NK5Y (old format)
Target: kintone 211 (希望収集), 212 (確定シフト), スタッフマスタ sheet

Steps:
  1. Add 米川登磨 S020 to スタッフマスタ sheet (if missing)
  2. Parse confirmed shifts (1-3月) → kintone 212
  3. Parse wish shifts (1-3月) → kintone 211

Usage:
    python3 migrate_legacy_data.py                   # dry-run (default)
    python3 migrate_legacy_data.py --no-dry-run      # execute
    python3 migrate_legacy_data.py --month 1         # January only
    python3 migrate_legacy_data.py --month 2,3       # Feb+Mar only
"""
import argparse
import base64
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta

from config import (
    KINTONE_DOMAIN, KINTONE_USERNAME, KINTONE_PASSWORD,
    KINTONE_SHIFT_WISH_APP_ID,
    SHIFT_SPREADSHEET_ID,
)

KINTONE_SHIFT_CONFIRMED_APP_ID = 212
SOURCE_SPREADSHEET_ID = "14g75SYgDPTtXXLq8CgGqAYtmp9hCP_jMHS-vOO_NK5Y"
GOOGLE_EMAIL = "satoru@chillaxy.jp"
YEAR = 2026

# Allowed targets (guardrails from CLAUDE.md)
ALLOWED_KINTONE_APPS = {211, 212, 213}
ALLOWED_SPREADSHEET_ID = "1JlyWngnuha1IHQLMGs5bzTnjct8s7eY4Z-bW0YHIlmU"

# ---------------------------------------------------------------------------
# Nickname → (full_name, staff_id) mapping
# ---------------------------------------------------------------------------
NICKNAME_MAP = {
    '智': ('小林智', 'S001'),
    '小林': ('小林智', 'S001'),
    '大塚': ('大塚裕斗', 'S002'),
    'モブ': ('石川宣位', 'S003'),
    'モグ': ('石川宣位', 'S003'),
    '石川': ('石川宣位', 'S003'),
    '稜也': ('工藤稜也', 'S004'),
    '工藤': ('工藤稜也', 'S004'),
    'あちゅ': ('松本明日香', 'S005'),
    'あちゅb': ('松本明日香', 'S005'),
    '松本': ('松本明日香', 'S005'),
    'ラスノブ': ('内藤信忠', 'S006'),
    '内藤': ('内藤信忠', 'S006'),
    '髙橋': ('髙橋舞人', 'S007'),
    '西村': ('西村瑠依', 'S008'),
    '下川': ('下川諒', 'S010'),
    '笠間': ('笠間璃久斗', 'S011'),
    '上岡': ('上岡泰己', 'S013'),
    '森': ('森さくら', 'S014'),
    '椿': ('椿昂大', 'S015'),
    '池田': ('池田雅', 'S016'),
    '石田': ('石田岳生', 'S018'),
    '牧田': ('牧田健之介', 'S019'),
    '米川': ('米川登磨', 'S020'),
}

# Names to skip (external staff)
SKIP_NAMES = {'きまる'}

# All active staff IDs for 公休 generation
ALL_STAFF_IDS = {
    'S001', 'S002', 'S003', 'S004', 'S005', 'S006', 'S007', 'S008',
    'S010', 'S011', 'S013', 'S014', 'S015', 'S016', 'S018', 'S019', 'S020',
}

# Staff IDs to skip in confirmed shifts (kintone 212) - no longer working
SKIP_CONFIRMED_STAFF = {'S014'}  # 森さくら

# Reverse map: staff_id → (full_name, staff_id)
STAFF_ID_TO_NAME = {}
_seen_ids = set()
for _nick, (_fname, _sid) in NICKNAME_MAP.items():
    if _sid not in _seen_ids:
        STAFF_ID_TO_NAME[_sid] = _fname
        _seen_ids.add(_sid)

# Store name normalization for kintone
STORE_NORMALIZE = {
    '藤沢': '藤沢',
    '伊勢佐木': '伊勢佐木町',
    '伊勢佐木町': '伊勢佐木町',
    '新宿': '新宿',
    '工場': '工場',
    '本部': '本部オフィス',
    '本部オフィス': '本部オフィス',
}


# ---------------------------------------------------------------------------
# Google OAuth helpers (same pattern as generate_shift.py)
# ---------------------------------------------------------------------------

def _find_google_credential():
    base_dir = os.path.expanduser("~/.google_workspace_mcp")
    for subdir in ["chillaxy", "gw-chillaxy"]:
        target = os.path.join(base_dir, subdir, f"{GOOGLE_EMAIL}.json")
        if os.path.exists(target):
            return target
    if os.path.isdir(base_dir):
        for subdir in os.listdir(base_dir):
            path = os.path.join(base_dir, subdir, f"{GOOGLE_EMAIL}.json")
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


def _get_token():
    cred_path = _find_google_credential()
    if not cred_path:
        raise RuntimeError("No Google OAuth credential found for " + GOOGLE_EMAIL)
    return _get_access_token(cred_path)


# ---------------------------------------------------------------------------
# Google Sheets API
# ---------------------------------------------------------------------------

def sheets_read(spreadsheet_id, range_name):
    """Read from any Google Sheets spreadsheet using batchGet (handles / in sheet names)."""
    token = _get_token()
    encoded_range = urllib.parse.quote(range_name)
    url = (
        f"https://sheets.googleapis.com/v4/spreadsheets/{spreadsheet_id}"
        f"/values:batchGet?ranges={encoded_range}"
    )
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req) as resp:
        data = json.loads(resp.read())
    value_ranges = data.get("valueRanges", [])
    if value_ranges:
        return value_ranges[0].get("values", [])
    return []


def sheets_append(spreadsheet_id, range_name, values):
    """Append rows to a Google Sheets spreadsheet."""
    assert spreadsheet_id == ALLOWED_SPREADSHEET_ID, \
        f"Guardrail: write only to allowed spreadsheet, got {spreadsheet_id}"
    token = _get_token()
    encoded_range = urllib.parse.quote(range_name)
    url = (
        f"https://sheets.googleapis.com/v4/spreadsheets/{spreadsheet_id}"
        f"/values/{encoded_range}:append"
        f"?valueInputOption=USER_ENTERED&insertDataOption=INSERT_ROWS"
    )
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    body = json.dumps({"values": values})
    req = urllib.request.Request(url, data=body.encode(), headers=headers, method="POST")
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


def kintone_get_records(app_id, query, fields=None):
    """Fetch records from kintone."""
    assert app_id in ALLOWED_KINTONE_APPS, f"Guardrail: app_id {app_id} not allowed"
    headers = _kintone_headers()
    body = {"app": app_id, "query": query + " limit 500", "totalCount": True}
    if fields:
        body["fields"] = fields
    data = json.dumps(body).encode()
    url = f"https://{KINTONE_DOMAIN}/k/v1/records.json"
    req = urllib.request.Request(url, data=data, headers=headers, method="GET")
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read())


def kintone_add_records(app_id, records):
    """Add multiple records to kintone (max 100)."""
    assert app_id in ALLOWED_KINTONE_APPS, f"Guardrail: app_id {app_id} not allowed"
    headers = _kintone_headers()
    url = f"https://{KINTONE_DOMAIN}/k/v1/records.json"
    body = {"app": app_id, "records": records}
    data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read())


def kintone_update_records(app_id, records):
    """Update multiple records in kintone (max 100)."""
    assert app_id in ALLOWED_KINTONE_APPS, f"Guardrail: app_id {app_id} not allowed"
    headers = _kintone_headers()
    url = f"https://{KINTONE_DOMAIN}/k/v1/records.json"
    body = {"app": app_id, "records": records}
    data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, headers=headers, method="PUT")
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read())


# ---------------------------------------------------------------------------
# Time normalization
# ---------------------------------------------------------------------------

def normalize_time(raw):
    """Normalize various time formats to HH:MM-HH:MM.

    Examples:
      '10-16'       → '10:00-16:00'
      '14〜23'      → '14:00-23:00'
      '15:30-0:30'  → '15:30-0:30'
      '11:30-16:00' → '11:30-16:00'
      '12-'         → '12:00-21:00'
      '×' / '△' / 'ー' → None (休み)
    """
    if not raw:
        return None
    raw = raw.strip()
    if raw in ('×', '△', 'ー', '休', '休み'):
        return None
    if raw.upper() in ('TRUE', 'FALSE'):
        return None

    # Replace fullwidth tilde / wave dash with hyphen
    raw = raw.replace('〜', '-').replace('~', '-').replace('～', '-')

    # Split on hyphen (but be careful with HH:MM-HH:MM)
    parts = raw.split('-', 1)
    if len(parts) < 2:
        return None

    start_raw = parts[0].strip()
    end_raw = parts[1].strip()

    # Handle '-22', '-23' etc. (end time only, default start 17:00)
    if not start_raw and end_raw:
        start_raw = '17'

    def pad_time(t):
        if not t:
            return None
        if ':' in t:
            h, m = t.split(':', 1)
            return f"{int(h)}:{m.zfill(2)}"
        try:
            return f"{int(t)}:00"
        except ValueError:
            return None

    start = pad_time(start_raw)
    if not start:
        return None

    if not end_raw:
        # Incomplete time like '12-' → default to 21:00 (office hours)
        end = "21:00"
    else:
        end = pad_time(end_raw)
        if not end:
            return None

    return f"{start}-{end}"


def parse_time_range(normalized):
    """Split normalized time range into (start_time, end_time)."""
    if not normalized or '-' not in normalized:
        return ('', '')
    parts = normalized.split('-', 1)
    return (parts[0].strip(), parts[1].strip())


# ---------------------------------------------------------------------------
# Resolve nickname to (staff_name, staff_id)
# ---------------------------------------------------------------------------

def resolve_nickname(nick):
    """Resolve a nickname to (full_name, staff_id) or None if skip/unknown."""
    if not nick:
        return None
    nick = nick.strip()
    if not nick or nick in SKIP_NAMES:
        return None
    if nick in ('ー', '×', '△', '', '-'):
        return None

    entry = NICKNAME_MAP.get(nick)
    if entry:
        return entry

    # Try stripping trailing whitespace/special chars
    for key, val in NICKNAME_MAP.items():
        if nick.startswith(key) or key.startswith(nick):
            return val

    return None


# ---------------------------------------------------------------------------
# STEP 1: Add 米川登磨 to スタッフマスタ
# ---------------------------------------------------------------------------

def step1_add_yonekawa(dry_run=True):
    """Add 米川登磨 S020 to スタッフマスタ sheet if not present."""
    print("\n[STEP 1] Check/add 米川登磨 S020 to スタッフマスタ...")

    rows = sheets_read(SHIFT_SPREADSHEET_ID, "スタッフマスタ!A1:N100")
    if not rows:
        print("  ERROR: スタッフマスタ sheet is empty")
        return

    # Check if S020 already exists
    for row in rows[1:]:
        if row and row[0] == 'S020':
            print("  S020 (米川登磨) already exists in スタッフマスタ, skipping.")
            return

    header = rows[0]
    print(f"  Header columns: {header}")

    # Build new row matching header order
    new_data = {
        'staff_id': 'S020',
        '氏名': '米川登磨',
        '雇用形態': 'アルバイト',
        '働き方': '希望シフト',
        '役職': '',
        '最大労働時間/週': '40',
        '個人ルール': '',
        'Slack ID': '',
        'LINE UID': '',
        '有効フラグ': '有効',
    }
    # Store checkbox columns
    store_defaults = {'藤沢': 'FALSE', '伊勢佐木町': 'TRUE', '新宿': 'FALSE', '工場': 'FALSE', '本部オフィス': 'FALSE'}

    new_row = []
    for col_name in header:
        if col_name in new_data:
            new_row.append(new_data[col_name])
        elif col_name in store_defaults:
            new_row.append(store_defaults[col_name])
        else:
            new_row.append('')

    print(f"  New row: {new_row}")

    if dry_run:
        print("  [dry-run] Would append row to スタッフマスタ")
    else:
        sheets_append(SHIFT_SPREADSHEET_ID, "スタッフマスタ!A1", [new_row])
        print("  Appended 米川登磨 S020 to スタッフマスタ")


# ---------------------------------------------------------------------------
# STEP 2: Parse confirmed shifts → kintone 212
# ---------------------------------------------------------------------------

def _get_cell(row, idx, default=''):
    """Safely get cell value from a row."""
    if idx < len(row):
        return str(row[idx]).strip()
    return default


def parse_jan_confirmed(rows):
    """Parse 2026/01 sheet (old format: 2 rows per day, no 早番/遅番 columns).

    Returns list of dicts: {staff_id, staff_name, date, store, shift_type, start_time, end_time}
    """
    records = []
    if len(rows) < 3:
        print("  WARNING: 2026/01 sheet has too few rows")
        return records

    # Row 1 (index 0): store headers
    # Row 2 (index 1): sub-headers (staff names for 本部 columns N-S)
    # Row 3+ (index 2+): data, 2 rows per day

    # Column mapping for January:
    # C(2)=date, D(3)=藤沢, E(4)=藤沢2, F(5)=伊勢佐木1, G(6)=伊勢佐木2, H(7)=伊勢佐木3
    # I(8)=新宿1, J(9)=新宿2, K(10)=新宿3, L(11)=工場1, M(12)=工場2
    # N(13)=智, O(14)=大塚, P(15)=あちゅ, Q(16)=椿, R(17)=西村, S(18)=きまる
    # T(19)=メモ, U(20)=休み

    store_cols = [
        # (col_idx, store_name, slot_label)
        (3, '藤沢', '通し'),
        (4, '藤沢', '通し'),
        (5, '伊勢佐木町', '通し'),
        (6, '伊勢佐木町', '通し'),
        (7, '伊勢佐木町', '通し'),
        (8, '新宿', '通し'),
        (9, '新宿', '通し'),
        (10, '新宿', '通し'),
        (11, '工場', '通し'),
        (12, '工場', '通し'),
    ]

    # 本部 individual columns from sub-header row
    honbu_cols = {}  # col_idx -> nickname
    if len(rows) > 1:
        sub_header = rows[1]
        for idx in range(13, min(19, len(sub_header))):
            nick = _get_cell(sub_header, idx)
            if nick and nick not in SKIP_NAMES:
                honbu_cols[idx] = nick

    # Parse 2-row-per-day structure
    data_start = 2
    i = data_start
    while i + 1 < len(rows):
        name_row = rows[i]
        time_row = rows[i + 1]

        # Get date from column C
        date_str = _get_cell(name_row, 2)
        if not date_str or '/' not in date_str:
            i += 2
            continue

        try:
            parts = date_str.split('/')
            month = int(parts[0])
            day = int(parts[1])
            full_date = f"{YEAR}-{month:02d}-{day:02d}"
        except (ValueError, IndexError):
            i += 2
            continue

        # Parse 休み column
        rest_names_raw = _get_cell(name_row, 20)
        rest_names = set()
        if rest_names_raw:
            for rn in re.split(r'[,、\s]+', rest_names_raw):
                rn = rn.strip()
                if rn:
                    rest_names.add(rn)

        # Track who's assigned to stores (to determine 本部 individual status)
        assigned_staff = set()

        # Parse store columns
        for col_idx, store_name, default_type in store_cols:
            nick = _get_cell(name_row, col_idx)
            if not nick or nick in ('ー', '×', '△', '-'):
                continue
            resolved = resolve_nickname(nick)
            if not resolved:
                continue
            staff_name, staff_id = resolved
            time_val = _get_cell(time_row, col_idx)
            normalized = normalize_time(time_val)
            start_t, end_t = parse_time_range(normalized) if normalized else ('', '')

            assigned_staff.add(nick)
            records.append({
                'staff_id': staff_id,
                'staff_name': staff_name,
                'date': full_date,
                'store': store_name,
                'shift_type': '通し',
                'start_time': start_t,
                'end_time': end_t,
                'status': '出勤',
            })

        # Parse 本部 individual columns
        for col_idx, nick in honbu_cols.items():
            cell_val = _get_cell(name_row, col_idx)
            time_val = _get_cell(time_row, col_idx)

            resolved = resolve_nickname(nick)
            if not resolved:
                continue
            staff_name, staff_id = resolved

            # Skip if in rest list or marked as ー
            if nick in rest_names:
                continue
            if cell_val in ('ー', '×', '△'):
                continue

            # If cell_val contains a store name, they're working at that store
            store_override = ''
            if cell_val:
                if cell_val in STORE_NORMALIZE:
                    store_override = STORE_NORMALIZE[cell_val]
                else:
                    # Check partial match (e.g., '藤沢OP' contains '藤沢')
                    for sname in STORE_NORMALIZE:
                        if sname in cell_val:
                            store_override = STORE_NORMALIZE[sname]
                            break

            # If they have a time value, they're working
            normalized = normalize_time(time_val)
            if normalized:
                start_t, end_t = parse_time_range(normalized)
                records.append({
                    'staff_id': staff_id,
                    'staff_name': staff_name,
                    'date': full_date,
                    'store': store_override or '本部オフィス',
                    'shift_type': '通し',
                    'start_time': start_t,
                    'end_time': end_t,
                    'status': '出勤',
                })
            elif cell_val and cell_val not in ('', 'ー', '×', '△'):
                # Check if cell_val itself is a time
                normalized = normalize_time(cell_val) if '-' in cell_val or '〜' in cell_val else None
                if normalized:
                    start_t, end_t = parse_time_range(normalized)
                    records.append({
                        'staff_id': staff_id,
                        'staff_name': staff_name,
                        'date': full_date,
                        'store': store_override or '本部オフィス',
                        'shift_type': '通し',
                        'start_time': start_t,
                        'end_time': end_t,
                        'status': '出勤',
                    })
                else:
                    # Name present but no time recorded - still create a record
                    records.append({
                        'staff_id': staff_id,
                        'staff_name': staff_name,
                        'date': full_date,
                        'store': store_override or '本部オフィス',
                        'shift_type': '通し',
                        'start_time': '',
                        'end_time': '',
                        'status': '出勤',
                    })

        i += 2

    return records


def parse_feb_mar_confirmed(rows, month):
    """Parse 2026/02 or 2026/03 sheet (new format: 1 row per day, 早番/遅番 + individual time columns).

    Dynamic column mapping: reads store headers (row 1) and staff names (row 2)
    to build the mapping. Handles:
    - Store assignment columns (早番/遅番): contain staff nicknames
    - Individual time columns: contain time ranges or store names
    - Same person in both store assignment AND individual time columns
    - Values like '伊勢佐木' in individual columns = store indicator, not time

    Returns list of dicts: {staff_id, staff_name, date, store, shift_type, start_time, end_time}
    """
    records = []
    if len(rows) < 4:
        print(f"  WARNING: 2026/{month:02d} sheet has too few rows")
        return records

    # Row 1 (index 0): store headers (merged cells, so value appears at leftmost col of group)
    store_header = rows[0]
    # Row 2 (index 1): sub-headers (早番/遅番 for store assignments, staff names for individual)
    sub_header = rows[1]

    # Build dynamic column mapping by scanning headers
    # Phase 1: Identify store assignment columns (早番/遅番)
    store_assignments = []  # (col_idx, store_name, shift_type)

    # Phase 2: Identify individual time columns (staff name → col_idx, store)
    indiv_cols = {}  # col_idx -> nickname
    indiv_store = {}  # col_idx -> store_name

    # Track current store context from row 1
    # Only recognized store names update the context (skip notes like ※早番はオレンジ文字)
    KNOWN_STORE_NAMES = set(STORE_NORMALIZE.keys()) | set(STORE_NORMALIZE.values()) | {'メモ'}
    current_store = ''
    for idx in range(4, max(len(store_header), len(sub_header))):
        # Update current store from row 1 (store headers span multiple columns)
        if idx < len(store_header):
            sh = store_header[idx].strip() if store_header[idx] else ''
            if sh and sh in KNOWN_STORE_NAMES and sh != 'メモ':
                current_store = STORE_NORMALIZE.get(sh, sh)

        sub_val = _get_cell(sub_header, idx) if idx < len(sub_header) else ''

        if sub_val == '早番':
            store_assignments.append((idx, current_store, '早番'))
        elif sub_val == '遅番':
            store_assignments.append((idx, current_store, '遅番'))
        elif sub_val and sub_val not in ('', '確定', '変更', 'メモ'):
            # This is a staff nickname in individual time section
            resolved = resolve_nickname(sub_val)
            if resolved:
                indiv_cols[idx] = sub_val
                indiv_store[idx] = current_store

    print(f"  Month {month} store assignments: {[(c, s, t) for c, s, t in store_assignments]}")
    print(f"  Month {month} individual columns: {[(c, n, indiv_store.get(c, '?')) for c, n in sorted(indiv_cols.items())]}")

    # Parse data rows (row 4+, index 3+)
    for row in rows[3:]:
        # Col C (index 2): date
        date_str = _get_cell(row, 2)
        if not date_str or '/' not in date_str:
            continue

        try:
            parts = date_str.split('/')
            m = int(parts[0])
            d = int(parts[1])
            full_date = f"{YEAR}-{m:02d}-{d:02d}"
        except (ValueError, IndexError):
            continue

        # Collect all records for this day, keyed by staff_id
        # Each entry: {staff_id, staff_name, date, store, shift_type, start_time, end_time, status}
        day_records = {}  # staff_id -> list of record dicts

        # Phase 1: Parse store assignment columns (contain staff nicknames)
        for col_idx, store_name, shift_type in store_assignments:
            nick = _get_cell(row, col_idx)
            if not nick:
                continue
            resolved = resolve_nickname(nick)
            if not resolved:
                continue
            staff_name, staff_id = resolved

            # Look up time from the individual columns for this staff
            start_t, end_t = '', ''
            for ic_idx, ic_nick in indiv_cols.items():
                ic_resolved = resolve_nickname(ic_nick)
                if ic_resolved and ic_resolved[1] == staff_id:
                    time_val = _get_cell(row, ic_idx)
                    # Skip if value is a store name (not a time)
                    if time_val and time_val not in STORE_NORMALIZE:
                        normalized = normalize_time(time_val)
                        if normalized:
                            start_t, end_t = parse_time_range(normalized)
                    break

            rec = {
                'staff_id': staff_id,
                'staff_name': staff_name,
                'date': full_date,
                'store': store_name,
                'shift_type': shift_type,
                'start_time': start_t,
                'end_time': end_t,
                'status': '出勤',
            }
            if staff_id not in day_records:
                day_records[staff_id] = []
            day_records[staff_id].append(rec)

        # Phase 2: Parse individual time columns
        # Staff may appear in BOTH store assignment AND individual columns (e.g., 髙橋 at 伊勢佐木 + 工場)
        # Only skip if same (staff_id, store) is already recorded
        assigned_stores = set()
        for sid, recs in day_records.items():
            for rec in recs:
                assigned_stores.add((sid, rec.get('store', '')))

        for col_idx, nick in indiv_cols.items():
            resolved = resolve_nickname(nick)
            if not resolved:
                continue
            staff_name, staff_id = resolved
            indiv_store_name = indiv_store.get(col_idx, '本部オフィス')
            if (staff_id, indiv_store_name) in assigned_stores:
                continue  # same staff+store already handled

            cell_val = _get_cell(row, col_idx)
            if not cell_val or cell_val in ('ー', '×', '△'):
                continue

            # Check if value is a store name (indicates where they work, not a time)
            if cell_val in STORE_NORMALIZE:
                # They work at this store, but no time recorded in this cell
                # Look for time in nearby columns for same staff (unlikely but handle)
                rec = {
                    'staff_id': staff_id,
                    'staff_name': staff_name,
                    'date': full_date,
                    'store': STORE_NORMALIZE[cell_val],
                    'shift_type': '通し',
                    'start_time': '',
                    'end_time': '',
                    'status': '出勤',
                }
                day_records.setdefault(staff_id, []).append(rec)
                continue

            normalized = normalize_time(cell_val)
            if not normalized:
                # Value exists but is not a time or store name (e.g., '渋谷', '藤沢OP', '15時', 'EC')
                # Treat as 出勤 with no time recorded; the value may be a location note
                store = indiv_store.get(col_idx, '本部オフィス')
                # Check if value looks like a store/location override
                for sname in STORE_NORMALIZE:
                    if sname in cell_val:
                        store = STORE_NORMALIZE[sname]
                        break
                rec = {
                    'staff_id': staff_id,
                    'staff_name': staff_name,
                    'date': full_date,
                    'store': store,
                    'shift_type': '通し',
                    'start_time': '',
                    'end_time': '',
                    'status': '出勤',
                }
                day_records.setdefault(staff_id, []).append(rec)
                continue

            start_t, end_t = parse_time_range(normalized)
            store = indiv_store.get(col_idx, '本部オフィス')

            rec = {
                'staff_id': staff_id,
                'staff_name': staff_name,
                'date': full_date,
                'store': store,
                'shift_type': '通し',
                'start_time': start_t,
                'end_time': end_t,
                'status': '出勤',
            }
            day_records.setdefault(staff_id, []).append(rec)

        # Flatten day_records into records list
        for staff_id, recs in day_records.items():
            records.extend(recs)

    return records


def step2_confirmed_shifts(months, dry_run=True):
    """Parse confirmed shifts from source spreadsheet and register to kintone 212."""
    print("\n[STEP 2] Parse confirmed shifts → kintone 212")

    all_records = []

    for month in months:
        sheet_name = f"{YEAR}/{month:02d}"
        print(f"\n  Reading {sheet_name}...")
        try:
            rows = sheets_read(SOURCE_SPREADSHEET_ID, f"'{sheet_name}'!A1:AW200")
        except urllib.error.HTTPError as e:
            print(f"  ERROR reading {sheet_name}: {e}")
            continue

        if not rows:
            print(f"  WARNING: {sheet_name} is empty")
            continue

        print(f"  Read {len(rows)} rows from {sheet_name}")

        if month == 1:
            records = parse_jan_confirmed(rows)
        else:
            records = parse_feb_mar_confirmed(rows, month)

        print(f"  Parsed {len(records)} shift records for month {month}")
        all_records.extend(records)

    if not all_records:
        print("  No records parsed from sheets")

    # Issue 5: Skip 森さくら (S014) from confirmed shifts
    all_records = [r for r in all_records if r['staff_id'] not in SKIP_CONFIRMED_STAFF]

    # Issue 4: Deduplicate by (staff_id, date) - merge times for duplicates
    deduped = {}
    for r in all_records:
        key = (r['staff_id'], r['date'])
        if key not in deduped:
            deduped[key] = r
        else:
            existing = deduped[key]
            # Merge: keep store assignment (早番/遅番) over 通し; take earliest start, latest end
            if r['shift_type'] in ('早番', '遅番') and existing['shift_type'] == '通し':
                # New record has store assignment, prefer it but merge times
                merged_start = existing['start_time']
                merged_end = existing['end_time']
                if r['start_time'] and merged_start:
                    merged_start = min(r['start_time'], merged_start)
                elif r['start_time']:
                    merged_start = r['start_time']
                if r['end_time'] and merged_end:
                    merged_end = max(r['end_time'], merged_end)
                elif r['end_time']:
                    merged_end = r['end_time']
                r['start_time'] = merged_start
                r['end_time'] = merged_end
                deduped[key] = r
            else:
                # Merge times into existing
                if r['start_time'] and existing['start_time']:
                    existing['start_time'] = min(existing['start_time'], r['start_time'])
                elif r['start_time']:
                    existing['start_time'] = r['start_time']
                if r['end_time'] and existing['end_time']:
                    existing['end_time'] = max(existing['end_time'], r['end_time'])
                elif r['end_time']:
                    existing['end_time'] = r['end_time']

    all_records = list(deduped.values())

    # Issue 3: Generate 公休 records for ALL staff on ALL days where they have no assignment
    import calendar
    for month in months:
        num_days = calendar.monthrange(YEAR, month)[1]
        for day in range(1, num_days + 1):
            full_date = f"{YEAR}-{month:02d}-{day:02d}"
            # Find which staff already have records for this day
            assigned_ids = {r['staff_id'] for r in all_records if r['date'] == full_date}
            # Generate 公休 for missing staff (skip SKIP_CONFIRMED_STAFF)
            for sid in ALL_STAFF_IDS - SKIP_CONFIRMED_STAFF:
                if sid not in assigned_ids:
                    staff_name = STAFF_ID_TO_NAME.get(sid, sid)
                    all_records.append({
                        'staff_id': sid,
                        'staff_name': staff_name,
                        'date': full_date,
                        'store': '',
                        'shift_type': '',
                        'start_time': '',
                        'end_time': '',
                        'status': '公休',
                    })

    print(f"\n  Total records after dedup + 公休: {len(all_records)}")

    # Log sample records
    work_count = sum(1 for r in all_records if r['status'] == '出勤')
    rest_count = sum(1 for r in all_records if r['status'] != '出勤')
    print(f"  出勤: {work_count}, 公休: {rest_count}")

    # Show per-staff summary
    staff_summary = {}
    for r in all_records:
        sid = r['staff_id']
        if sid not in staff_summary:
            staff_summary[sid] = {'name': r['staff_name'], 'work': 0, 'rest': 0}
        if r['status'] == '出勤':
            staff_summary[sid]['work'] += 1
        else:
            staff_summary[sid]['rest'] += 1

    print("\n  Per-staff summary:")
    for sid in sorted(staff_summary.keys()):
        s = staff_summary[sid]
        print(f"    {sid} {s['name']}: 出勤={s['work']}日, 公休={s['rest']}日")

    # Fetch existing records from kintone for upsert
    print("\n  Fetching existing kintone 212 records...")
    existing = {}
    for month in months:
        start_date = f"{YEAR}-{month:02d}-01"
        if month == 12:
            end_date = f"{YEAR + 1}-01-01"
        else:
            end_date = f"{YEAR}-{month + 1:02d}-01"
        query = f'shift_date >= "{start_date}" and shift_date < "{end_date}" order by staff_id asc, shift_date asc'
        try:
            result = kintone_get_records(
                KINTONE_SHIFT_CONFIRMED_APP_ID, query,
                fields=["$id", "staff_id", "shift_date"]
            )
            for rec in result.get("records", []):
                sid = rec.get("staff_id", {}).get("value", "")
                sd = rec.get("shift_date", {}).get("value", "")
                rid = rec["$id"]["value"]
                if sid and sd:
                    existing[(sid, sd)] = rid
        except urllib.error.HTTPError as e:
            print(f"  WARNING: Could not fetch existing records for month {month}: {e}")

    print(f"  Existing records in kintone 212: {len(existing)}")

    # Build kintone records
    to_add = []
    to_update = []

    for r in all_records:
        kintone_rec = {
            "staff_id": {"value": r['staff_id']},
            "staff_name": {"value": r['staff_name']},
            "shift_date": {"value": r['date']},
            "shift_status": {"value": r['status']},
        }
        if r['store']:
            kintone_rec["store"] = {"value": r['store']}
        if r['start_time']:
            kintone_rec["start_time"] = {"value": r['start_time']}
        if r['end_time']:
            kintone_rec["end_time"] = {"value": r['end_time']}
        if r['shift_type']:
            kintone_rec["shift_type"] = {"value": r['shift_type']}

        # period_start and period_end for the whole month
        month_num = int(r['date'].split('-')[1])
        import calendar
        last_day = calendar.monthrange(YEAR, month_num)[1]
        kintone_rec["period_start"] = {"value": f"{YEAR}-{month_num:02d}-01"}
        kintone_rec["period_end"] = {"value": f"{YEAR}-{month_num:02d}-{last_day:02d}"}

        key = (r['staff_id'], r['date'])
        record_id = existing.get(key)
        if record_id:
            to_update.append({"id": record_id, "record": kintone_rec})
        else:
            to_add.append(kintone_rec)

    print(f"\n  To add: {len(to_add)}, To update: {len(to_update)}")

    if dry_run:
        print("  [dry-run] Skipping kintone writes")
        # Print first 5 records as sample
        print("\n  Sample records to add:")
        for r in to_add[:5]:
            vals = {k: v['value'] for k, v in r.items()}
            print(f"    {vals}")
        if len(to_add) > 5:
            print(f"    ... and {len(to_add) - 5} more")
    else:
        added = 0
        for i in range(0, len(to_add), 100):
            batch = to_add[i:i + 100]
            kintone_add_records(KINTONE_SHIFT_CONFIRMED_APP_ID, batch)
            added += len(batch)
            print(f"  Added batch: {added}/{len(to_add)}")

        updated = 0
        for i in range(0, len(to_update), 100):
            batch = to_update[i:i + 100]
            kintone_update_records(KINTONE_SHIFT_CONFIRMED_APP_ID, batch)
            updated += len(batch)
            print(f"  Updated batch: {updated}/{len(to_update)}")

        print(f"\n  kintone 212 registration complete: added={added}, updated={updated}")


# ---------------------------------------------------------------------------
# STEP 3: Parse wish shifts → kintone 211
# ---------------------------------------------------------------------------

def parse_wish_sheet_main(rows):
    """Parse '希望シフト' sheet (main).

    Row 1: header ['', '椿', '上岡', '池田', '牧田', '森']
    Row 2+: data (date label in col A sometimes missing for first row)
    Data range: 2/1 through 3/23 (ignore 5/1+)
    """
    records = []
    if not rows or len(rows) < 2:
        return records

    header = rows[0]
    staff_nicks = []
    for idx in range(1, len(header)):
        nick = header[idx].strip() if idx < len(header) else ''
        if nick:
            staff_nicks.append((idx, nick))

    print(f"  希望シフト sheet staff: {[n for _, n in staff_nicks]}")

    # Parse rows - first data row may lack date label
    current_month = 2
    current_day = 1

    for row_idx in range(1, len(rows)):
        row = rows[row_idx]
        date_label = _get_cell(row, 0)

        if date_label and '/' in date_label:
            try:
                parts = date_label.split('/')
                current_month = int(parts[0])
                current_day = int(parts[1])
            except (ValueError, IndexError):
                continue
        elif row_idx == 1:
            # First data row - assumed to be 2/1 if no date label
            current_month = 2
            current_day = 1
        else:
            # Increment day from previous
            current_day += 1
            # Handle month rollover
            import calendar
            max_day = calendar.monthrange(YEAR, current_month)[1]
            if current_day > max_day:
                current_month += 1
                current_day = 1

        # Skip May onwards
        if current_month >= 5:
            continue
        # Only process Jan-Mar
        if current_month > 3:
            continue

        full_date = f"{YEAR}-{current_month:02d}-{current_day:02d}"

        for col_idx, nick in staff_nicks:
            resolved = resolve_nickname(nick)
            if not resolved:
                continue
            staff_name, staff_id = resolved

            val = _get_cell(row, col_idx)
            if not val:
                continue

            if val in ('×', '△', 'ー', '休', '休み'):
                records.append({
                    'staff_id': staff_id,
                    'staff_name': staff_name,
                    'date': full_date,
                    'shift_type': '休み',
                    'start_time': '',
                    'end_time': '',
                })
            else:
                normalized = normalize_time(val)
                if normalized:
                    start_t, end_t = parse_time_range(normalized)
                    records.append({
                        'staff_id': staff_id,
                        'staff_name': staff_name,
                        'date': full_date,
                        'shift_type': '出勤',
                        'start_time': start_t,
                        'end_time': end_t,
                    })

    return records


def parse_wish_sheet_trailing(rows):
    """Parse '希望シフト ' sheet (trailing space, older data).

    Row 1: ['zs', '椿', '上岡', '森', '柏木', '三澤']
    Rows 97-127: January data (1/1-1/31)
    """
    records = []
    if not rows or len(rows) < 2:
        return records

    header = rows[0]
    staff_nicks = []
    for idx in range(1, len(header)):
        nick = header[idx].strip() if idx < len(header) else ''
        if nick:
            resolved = resolve_nickname(nick)
            if resolved:
                staff_nicks.append((idx, nick))

    print(f"  希望シフト (trailing) staff: {[n for _, n in staff_nicks]}")

    if not staff_nicks:
        return records

    # Look for January data - scan rows for 1/1 through 1/31
    for row_idx in range(1, len(rows)):
        row = rows[row_idx]
        date_label = _get_cell(row, 0)

        if not date_label or '/' not in date_label:
            continue

        try:
            parts = date_label.split('/')
            month = int(parts[0])
            day = int(parts[1])
        except (ValueError, IndexError):
            continue

        # Only process January
        if month != 1:
            continue

        full_date = f"{YEAR}-01-{day:02d}"

        for col_idx, nick in staff_nicks:
            resolved = resolve_nickname(nick)
            if not resolved:
                continue
            staff_name, staff_id = resolved

            val = _get_cell(row, col_idx)
            if not val:
                continue

            if val in ('×', '△', 'ー', '休', '休み'):
                records.append({
                    'staff_id': staff_id,
                    'staff_name': staff_name,
                    'date': full_date,
                    'shift_type': '休み',
                    'start_time': '',
                    'end_time': '',
                })
            else:
                normalized = normalize_time(val)
                if normalized:
                    start_t, end_t = parse_time_range(normalized)
                    records.append({
                        'staff_id': staff_id,
                        'staff_name': staff_name,
                        'date': full_date,
                        'shift_type': '出勤',
                        'start_time': start_t,
                        'end_time': end_t,
                    })

    return records


def parse_wish_2026_section(rows):
    """Parse 2026 wish data from Row 301+ of main 希望シフト sheet.

    Columns: A=date, B=椿, C=上岡, D=池田, E=牧田
    Staff mapping uses header from original Row 1 (B=椿, C=上岡, D=池田, E=牧田).
    Time formats: '10-16', '11:30〜16', '-22', '-23', '14:30-23:30', '×', 'x'
    """
    records = []
    staff_cols = [
        (1, '椿', 'S015', '椿昂大'),
        (2, '上岡', 'S013', '上岡泰己'),
        (3, '池田', 'S016', '池田雅'),
        (4, '牧田', 'S019', '牧田健之介'),
    ]

    for row in rows:
        date_label = _get_cell(row, 0)
        if not date_label or '/' not in date_label:
            continue
        # Skip currency-formatted dates (¥46,023.00 etc.)
        if '¥' in date_label or ',' in date_label:
            continue

        try:
            parts = date_label.split('/')
            month = int(parts[0])
            day = int(parts[1])
        except (ValueError, IndexError):
            continue

        # Only Jan-Mar 2026
        if month < 1 or month > 3:
            continue

        full_date = f"{YEAR}-{month:02d}-{day:02d}"

        for col_idx, nick, staff_id, staff_name in staff_cols:
            val = _get_cell(row, col_idx)
            if not val:
                continue

            # Handle lowercase x
            if val.lower() == 'x' or val in ('×', '△', 'ー', '休', '休み'):
                records.append({
                    'staff_id': staff_id,
                    'staff_name': staff_name,
                    'date': full_date,
                    'shift_type': '休み',
                    'start_time': '',
                    'end_time': '',
                })
            else:
                normalized = normalize_time(val)
                if normalized:
                    start_t, end_t = parse_time_range(normalized)
                    records.append({
                        'staff_id': staff_id,
                        'staff_name': staff_name,
                        'date': full_date,
                        'shift_type': '出勤',
                        'start_time': start_t,
                        'end_time': end_t,
                    })

    return records


def parse_wish_mori_only(rows):
    """Parse 森さくら (col F=5) data from the top section of main 希望シフト sheet.

    Row 1 = header, Row 2+ = data (2/1 onwards)
    Only extracts 森さくら (col F, index 5), Jan-Mar.
    """
    records = []
    if not rows or len(rows) < 2:
        return records

    staff_id = 'S014'
    staff_name = '森さくら'

    current_month = 2
    current_day = 1

    for row_idx in range(1, len(rows)):
        row = rows[row_idx]
        date_label = _get_cell(row, 0)

        if date_label and '/' in date_label:
            try:
                parts = date_label.split('/')
                current_month = int(parts[0])
                current_day = int(parts[1])
            except (ValueError, IndexError):
                continue
        elif row_idx == 1:
            current_month = 2
            current_day = 1
        else:
            current_day += 1
            import calendar
            max_day = calendar.monthrange(YEAR, current_month)[1]
            if current_day > max_day:
                current_month += 1
                current_day = 1

        if current_month > 3:
            continue

        full_date = f"{YEAR}-{current_month:02d}-{current_day:02d}"

        val = _get_cell(row, 5)  # Col F = 森
        if not val:
            continue

        if val in ('×', '△', 'ー', '休', '休み'):
            records.append({
                'staff_id': staff_id,
                'staff_name': staff_name,
                'date': full_date,
                'shift_type': '休み',
                'start_time': '',
                'end_time': '',
            })
        else:
            normalized = normalize_time(val)
            if normalized:
                start_t, end_t = parse_time_range(normalized)
                records.append({
                    'staff_id': staff_id,
                    'staff_name': staff_name,
                    'date': full_date,
                    'shift_type': '出勤',
                    'start_time': start_t,
                    'end_time': end_t,
                })

    return records


def step3_wish_shifts(months, dry_run=True):
    """Parse wish shifts from source spreadsheet and register to kintone 211."""
    print("\n[STEP 3] Parse wish shifts → kintone 211")

    all_records = []

    # 2026 hope shift data is at Row 301+ for 椿/上岡/池田/牧田
    print("\n  Reading '希望シフト' Row 301+ (2026 data: 椿, 上岡, 池田, 牧田)...")
    try:
        rows = sheets_read(SOURCE_SPREADSHEET_ID, "'希望シフト'!A301:F500")
        wish_records = parse_wish_2026_section(rows)
        print(f"  Parsed {len(wish_records)} wish records from Row 301+")
        all_records.extend(wish_records)
    except urllib.error.HTTPError as e:
        print(f"  ERROR reading 希望シフト Row 301+: {e}")

    # 森さくら (S014) - もう出勤していないのでスキップ
    print("\n  森さくら (S014): スキップ（退職済み）")

    # Filter by requested months
    filtered = []
    for r in all_records:
        month = int(r['date'].split('-')[1])
        if month in months:
            filtered.append(r)
    all_records = filtered

    if not all_records:
        print("  No wish records to register")
        return

    # Deduplicate by (staff_id, date) - keep last
    deduped = {}
    for r in all_records:
        key = (r['staff_id'], r['date'])
        deduped[key] = r
    all_records = list(deduped.values())

    print(f"\n  Total wish records after dedup: {len(all_records)}")

    # Per-staff summary
    staff_summary = {}
    for r in all_records:
        sid = r['staff_id']
        if sid not in staff_summary:
            staff_summary[sid] = {'name': r['staff_name'], 'work': 0, 'off': 0}
        if r['shift_type'] == '出勤':
            staff_summary[sid]['work'] += 1
        else:
            staff_summary[sid]['off'] += 1

    print("\n  Per-staff wish summary:")
    for sid in sorted(staff_summary.keys()):
        s = staff_summary[sid]
        print(f"    {sid} {s['name']}: 出勤希望={s['work']}日, 休み希望={s['off']}日")

    # Fetch existing records from kintone for upsert
    print("\n  Fetching existing kintone 211 records...")
    existing = {}
    for month in months:
        start_date = f"{YEAR}-{month:02d}-01"
        if month == 12:
            end_date = f"{YEAR + 1}-01-01"
        else:
            end_date = f"{YEAR}-{month + 1:02d}-01"
        query = f'shift_date >= "{start_date}" and shift_date < "{end_date}" order by staff_id asc, shift_date asc'
        try:
            result = kintone_get_records(
                KINTONE_SHIFT_WISH_APP_ID, query,
                fields=["$id", "staff_id", "shift_date"]
            )
            for rec in result.get("records", []):
                sid = rec.get("staff_id", {}).get("value", "")
                sd = rec.get("shift_date", {}).get("value", "")
                rid = rec["$id"]["value"]
                if sid and sd:
                    existing[(sid, sd)] = rid
        except urllib.error.HTTPError as e:
            print(f"  WARNING: Could not fetch existing records for month {month}: {e}")

    print(f"  Existing records in kintone 211: {len(existing)}")

    # Build kintone records
    to_add = []
    to_update = []

    for r in all_records:
        month_num = int(r['date'].split('-')[1])
        import calendar
        last_day = calendar.monthrange(YEAR, month_num)[1]

        kintone_rec = {
            "staff_id": {"value": r['staff_id']},
            "staff_name": {"value": r['staff_name']},
            "shift_date": {"value": r['date']},
            "shift_type": {"value": r['shift_type']},
            "input_status": {"value": "入力済"},
            "input_channel": {"value": "スプレッドシート"},
            "target_period_start": {"value": f"{YEAR}-{month_num:02d}-01"},
            "target_period_end": {"value": f"{YEAR}-{month_num:02d}-{last_day:02d}"},
        }
        if r['start_time']:
            kintone_rec["start_time"] = {"value": r['start_time']}
        if r['end_time']:
            kintone_rec["end_time"] = {"value": r['end_time']}

        key = (r['staff_id'], r['date'])
        record_id = existing.get(key)
        if record_id:
            to_update.append({"id": record_id, "record": kintone_rec})
        else:
            to_add.append(kintone_rec)

    print(f"\n  To add: {len(to_add)}, To update: {len(to_update)}")

    if dry_run:
        print("  [dry-run] Skipping kintone writes")
        print("\n  Sample records to add:")
        for r in to_add[:5]:
            vals = {k: v['value'] for k, v in r.items()}
            print(f"    {vals}")
        if len(to_add) > 5:
            print(f"    ... and {len(to_add) - 5} more")
    else:
        added = 0
        for i in range(0, len(to_add), 100):
            batch = to_add[i:i + 100]
            kintone_add_records(KINTONE_SHIFT_WISH_APP_ID, batch)
            added += len(batch)
            print(f"  Added batch: {added}/{len(to_add)}")

        updated = 0
        for i in range(0, len(to_update), 100):
            batch = to_update[i:i + 100]
            kintone_update_records(KINTONE_SHIFT_WISH_APP_ID, batch)
            updated += len(batch)
            print(f"  Updated batch: {updated}/{len(to_update)}")

        print(f"\n  kintone 211 registration complete: added={added}, updated={updated}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Migrate legacy shift data to new system")
    parser.add_argument(
        '--no-dry-run', action='store_true',
        help='Execute writes (default is dry-run)'
    )
    parser.add_argument(
        '--month', type=str, default='1,2,3',
        help='Comma-separated months to process (default: 1,2,3)'
    )
    parser.add_argument(
        '--step', type=str, default='1,2,3',
        help='Comma-separated steps to run (default: 1,2,3)'
    )
    args = parser.parse_args()

    dry_run = not args.no_dry_run
    months = [int(m.strip()) for m in args.month.split(',')]
    steps = [int(s.strip()) for s in args.step.split(',')]

    mode = "DRY-RUN" if dry_run else "LIVE"
    print(f"=== Legacy Data Migration ({mode}) ===")
    print(f"  Source: {SOURCE_SPREADSHEET_ID}")
    print(f"  Target SS: {SHIFT_SPREADSHEET_ID}")
    print(f"  Target kintone: 211 (wishes), 212 (confirmed)")
    print(f"  Months: {months}")
    print(f"  Steps: {steps}")

    # Guardrail checks
    assert SHIFT_SPREADSHEET_ID == ALLOWED_SPREADSHEET_ID, \
        f"Guardrail: SHIFT_SPREADSHEET_ID mismatch: {SHIFT_SPREADSHEET_ID}"
    assert KINTONE_SHIFT_WISH_APP_ID in ALLOWED_KINTONE_APPS, \
        f"Guardrail: KINTONE_SHIFT_WISH_APP_ID={KINTONE_SHIFT_WISH_APP_ID} not allowed"

    try:
        if 1 in steps:
            step1_add_yonekawa(dry_run=dry_run)

        if 2 in steps:
            step2_confirmed_shifts(months, dry_run=dry_run)

        if 3 in steps:
            step3_wish_shifts(months, dry_run=dry_run)

        print(f"\n=== Migration complete ({mode}) ===")

    except Exception as e:
        print(f"\nERROR: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
