#!/usr/bin/env python3
"""Sync staff data from SmartHR → スタッフマスタ spreadsheet.

Pulls employed crew members from SmartHR API and updates the staff master
spreadsheet. Matches by 氏名 (name) for existing rows or appends new rows.
Does NOT overwrite manually-set fields (対応店舗, 働き方, 最大労働時間/週, etc.).

Usage:
    python3 sync_smarthr.py              # dry-run (default)
    python3 sync_smarthr.py --no-dry-run # execute sync
"""
import json
import os
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timedelta

from config import (
    SMARTHR_SUBDOMAIN,
    SMARTHR_ACCESS_TOKEN,
    SHIFT_SPREADSHEET_ID,
)

# Staff master columns (A-N):
# staff_id / 氏名 / 雇用形態 / 働き方 / 役職 / Slack ID / LINE UID / 有効フラグ / 藤沢 / 伊勢佐木町 / 新宿 / 工場 / 本部オフィス / 個人ルール
HEADER = [
    "staff_id", "氏名", "雇用形態", "働き方", "役職",
    "Slack ID", "LINE UID", "有効フラグ",
    "藤沢", "伊勢佐木町", "新宿", "工場", "本部オフィス",
    "個人ルール",
]

# Fields that SmartHR can populate (others are manual-only)
SMARTHR_MANAGED_FIELDS = {"氏名", "雇用形態", "役職", "有効フラグ"}

# SmartHR emp_type → 雇用形態 mapping
EMP_TYPE_MAP = {
    "board_member": "役員",
    "full_timer": "正社員",
    "contract_worker": "契約社員",
    "part_timer": "アルバイト",
}


# ---------------------------------------------------------------------------
# Google credential helpers (same as sync_staff_master.py)
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


def _get_google_token():
    cred_path = _find_google_credential()
    if not cred_path:
        raise RuntimeError("No Google OAuth credential found for satoru@chillaxy.jp")
    return _get_access_token(cred_path)


# ---------------------------------------------------------------------------
# Google Sheets API
# ---------------------------------------------------------------------------

def sheets_read(range_name):
    token = _get_google_token()
    encoded_range = urllib.parse.quote(range_name)
    url = (
        f"https://sheets.googleapis.com/v4/spreadsheets/{SHIFT_SPREADSHEET_ID}"
        f"/values/{encoded_range}"
    )
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req) as resp:
        data = json.loads(resp.read())
    return data.get("values", [])


def sheets_update(range_name, values):
    token = _get_google_token()
    encoded_range = urllib.parse.quote(range_name)
    url = (
        f"https://sheets.googleapis.com/v4/spreadsheets/{SHIFT_SPREADSHEET_ID}"
        f"/values/{encoded_range}?valueInputOption=USER_ENTERED"
    )
    body = {"range": range_name, "majorDimension": "ROWS", "values": values}
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode(),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        method="PUT",
    )
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read())


# ---------------------------------------------------------------------------
# SmartHR API
# ---------------------------------------------------------------------------

def smarthr_get_crews(emp_status="employed"):
    """Fetch all crew members from SmartHR."""
    if not SMARTHR_SUBDOMAIN or not SMARTHR_ACCESS_TOKEN:
        raise RuntimeError("SMARTHR_SUBDOMAIN / SMARTHR_ACCESS_TOKEN not set")

    all_crews = []
    page = 1
    while True:
        url = (
            f"https://{SMARTHR_SUBDOMAIN}.smarthr.jp/api/v1/crews"
            f"?per_page=100&page={page}&emp_status={emp_status}"
        )
        req = urllib.request.Request(
            url, headers={"Authorization": f"Bearer {SMARTHR_ACCESS_TOKEN}"}
        )
        with urllib.request.urlopen(req) as resp:
            data = json.loads(resp.read())
        if not data:
            break
        all_crews.extend(data)
        if len(data) < 100:
            break
        page += 1
    return all_crews


def parse_smarthr_crew(crew):
    """Convert a SmartHR crew record to staff master fields."""
    name = f"{crew.get('last_name', '')}{crew.get('first_name', '')}"

    emp_type = crew.get("emp_type", "")
    employment_type_obj = crew.get("employment_type") or {}
    employment_type = employment_type_obj.get("name", "") if isinstance(employment_type_obj, dict) else ""
    # Normalize: SmartHR says "アルバイト・パート" → we use "アルバイト"
    if not employment_type:
        employment_type = EMP_TYPE_MAP.get(emp_type, emp_type)
    elif "アルバイト" in employment_type:
        employment_type = "アルバイト"

    position_obj = crew.get("position")
    position = ""
    if isinstance(position_obj, str):
        position = position_obj
    elif isinstance(position_obj, dict) and position_obj:
        position = position_obj.get("name", "")

    emp_status = crew.get("emp_status", "")
    active = "有効" if emp_status == "employed" else "無効"

    return {
        "氏名": name,
        "雇用形態": employment_type,
        "役職": position or "スタッフ",
        "有効フラグ": active,
        "emp_code": crew.get("emp_code", ""),
        "email": crew.get("email", ""),
    }


# ---------------------------------------------------------------------------
# Sync logic
# ---------------------------------------------------------------------------

def next_staff_id(existing_ids):
    """Generate next S### staff_id."""
    max_num = -1
    for sid in existing_ids:
        if sid and sid.startswith("S") and sid[1:].isdigit():
            max_num = max(max_num, int(sid[1:]))
    return f"S{max_num + 1:03d}"


def sync(dry_run=True, replace_all=False):
    print("=== SmartHR → スタッフマスタ Sync ===\n")
    print(f"  Spreadsheet: {SHIFT_SPREADSHEET_ID}")
    print(f"  SmartHR subdomain: {SMARTHR_SUBDOMAIN}")
    print(f"  mode: {'REPLACE ALL' if replace_all else 'merge'}")
    print(f"  dry-run: {dry_run}\n")

    # 1. Read current spreadsheet (for row count / clear)
    print("[1/3] Reading current スタッフマスタ...")
    rows = sheets_read("スタッフマスタ!A1:N100")
    old_row_count = len(rows) if rows else 1
    print(f"  Current rows: {old_row_count - 1} (excluding header)")

    # 2. Fetch SmartHR
    print("[2/3] Fetching SmartHR employees...")
    employed = smarthr_get_crews("employed")
    retired = smarthr_get_crews("retired")
    all_crews = employed + retired
    print(f"  SmartHR: {len(employed)} employed, {len(retired)} retired")

    # 3. Build new staff list
    print("[3/3] Building staff master...\n")

    if replace_all:
        # Sort: board_member first, then by emp_code
        type_order = {"board_member": 0, "full_timer": 1, "contract_worker": 2, "part_timer": 3}
        all_crews.sort(key=lambda c: (
            type_order.get(c.get("emp_type", ""), 9),
            c.get("emp_code", "9999"),
        ))

        staff_list = []
        sid_num = 1
        for crew in all_crews:
            parsed = parse_smarthr_crew(crew)
            sid = f"S{sid_num:03d}"
            sid_num += 1

            row = {h: "" for h in HEADER}
            row["staff_id"] = sid
            row["氏名"] = parsed["氏名"]
            row["雇用形態"] = parsed["雇用形態"]
            row["役職"] = parsed["役職"]
            row["有効フラグ"] = parsed["有効フラグ"]
            row["働き方"] = "希望シフト"

            # Owner gets Slack ID
            if parsed.get("email") == "satoru@chillaxy.jp":
                row["Slack ID"] = "U07UBN61QFN"
                row["個人ルール"] = "オーナー。全店舗対応可。"

            staff_list.append(row)
            status = parsed["有効フラグ"]
            marker = "" if status == "有効" else " [無効]"
            print(f"    {sid} {parsed['氏名']} ({parsed['雇用形態']}/{parsed['役職']}){marker}")

    else:
        # Merge mode (existing logic)
        headers_row = rows[0] if rows else HEADER
        existing = []
        for row in rows[1:]:
            while len(row) < len(headers_row):
                row.append("")
            d = {headers_row[i]: row[i] for i in range(len(headers_row))}
            existing.append(d)

        existing_names = {d["氏名"]: i for i, d in enumerate(existing) if d.get("氏名")}
        existing_ids = [d.get("staff_id", "") for d in existing]

        for crew in employed:
            parsed = parse_smarthr_crew(crew)
            name = parsed["氏名"]
            if name in existing_names:
                idx = existing_names[name]
                for field in SMARTHR_MANAGED_FIELDS:
                    new_val = parsed.get(field, "")
                    if new_val:
                        existing[idx][field] = new_val
            else:
                sid = next_staff_id(existing_ids)
                existing_ids.append(sid)
                new_row = {h: "" for h in HEADER}
                new_row["staff_id"] = sid
                new_row["氏名"] = parsed["氏名"]
                new_row["雇用形態"] = parsed["雇用形態"]
                new_row["役職"] = parsed["役職"]
                new_row["有効フラグ"] = parsed["有効フラグ"]
                new_row["働き方"] = "希望シフト"
                existing.append(new_row)
                existing_names[name] = len(existing) - 1

        for crew in retired:
            parsed = parse_smarthr_crew(crew)
            name = parsed["氏名"]
            if name in existing_names:
                existing[existing_names[name]]["有効フラグ"] = "無効"

        staff_list = existing

    print(f"\n  Total: {len(staff_list)} staff")

    if dry_run:
        print("  [dry-run] No changes written.")
        return

    # Build output
    output = [HEADER]
    for d in staff_list:
        output.append([d.get(h, "") for h in HEADER])

    # Clear old rows if new data is shorter
    if len(output) < old_row_count:
        clear_rows = [[""] * len(HEADER)] * (old_row_count - len(output))
        output.extend(clear_rows)

    range_name = f"スタッフマスタ!A1:N{len(output)}"
    print(f"\n  Writing {len(output)} rows to {range_name}...")
    result = sheets_update(range_name, output)
    print(f"  Done. Updated {result.get('updatedCells', '?')} cells.")


def main():
    dry_run = "--no-dry-run" not in sys.argv
    replace_all = "--replace-all" in sys.argv
    if "--help" in sys.argv:
        print(__doc__)
        return
    sync(dry_run=dry_run, replace_all=replace_all)


if __name__ == "__main__":
    main()
