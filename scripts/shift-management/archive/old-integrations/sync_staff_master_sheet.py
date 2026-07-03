#!/usr/bin/env python3
"""
① スタッフマスタ同期 (Python版 - GAS不要)

機能:
1. スタッフマスタシートから有効スタッフを読み取り
2. 希望収集データシートのヘッダーを更新 (希望シフト+固定シフト全員)
3. kintone 213 にスタッフマスタを同期
4. シフト出力シートの名前ドロップダウンを更新

Usage:
  python3 sync_staff_master_sheet.py           # dry-run
  python3 sync_staff_master_sheet.py --no-dry-run  # 本番実行
"""
import json, os, sys, urllib.parse, urllib.request, base64
from datetime import datetime

SSID = "1JlyWngnuha1IHQLMGs5bzTnjct8s7eY4Z-bW0YHIlmU"
WSID = 1764958489   # 希望収集データ sheetId
OSID = 1533022256   # シフト出力 sheetId

# kintone
KINTONE_DOMAIN = "ny76p.cybozu.com"
KINTONE_STAFF_APP = 213

# シフト出力レイアウト
OUTPUT_DSTART = 5
OUTPUT_DAYS = 31
RETAIL_COLS = {
    '藤沢':       {'早番': 4, '遅番': 6, '※予備': 8},
    '伊勢佐木町': {'早番': 10, '遅番': 12, '※予備': 14},
    '新宿':       {'早番': 16, '遅番': 18, '※予備': 20},
}
FACTORY_PERSON_COLS = [22, 24, 26]
OFFICE_PERSON_COLS = [28, 30, 32, 34, 36, 38]


def get_token():
    base = os.path.expanduser("~/.google_workspace_mcp")
    for sub in ["chillaxy", "gw-chillaxy"]:
        p = os.path.join(base, sub, "satoru@chillaxy.jp.json")
        if os.path.exists(p):
            with open(p) as f:
                cred = json.load(f)
            break
    else:
        raise RuntimeError("No cred")
    tok = cred.get("token") or cred.get("access_token")
    exp = cred.get("expiry") or cred.get("token_expiry")
    if tok and exp:
        try:
            ed = datetime.fromisoformat(exp.replace("Z", "+00:00"))
            if ed > datetime.now(ed.tzinfo):
                return tok
        except:
            pass
    rt, ci, cs = cred.get("refresh_token"), cred.get("client_id"), cred.get("client_secret")
    if rt and ci and cs:
        d = urllib.parse.urlencode({
            "grant_type": "refresh_token", "refresh_token": rt,
            "client_id": ci, "client_secret": cs
        }).encode()
        r = urllib.request.Request("https://oauth2.googleapis.com/token", data=d, method="POST")
        with urllib.request.urlopen(r) as resp:
            return json.loads(resp.read())["access_token"]
    return tok


def sheets_api(method, path, body=None):
    tok = get_token()
    url = f"https://sheets.googleapis.com/v4/spreadsheets/{SSID}{path}"
    hdr = {"Authorization": f"Bearer {tok}", "Content-Type": "application/json"}
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(url, data=data, headers=hdr, method=method)
    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        print(f"  ERR {e.code}: {e.read().decode()[:500]}")
        raise


def bu(reqs, label=""):
    total = 0
    for i in range(0, len(reqs), 50):
        r = sheets_api("POST", ":batchUpdate", {"requests": reqs[i:i+50]})
        total += len(r.get('replies', []))
    if label:
        print(f"  {label}: {total} ops")


def wv(rng, vals, opt="USER_ENTERED"):
    return sheets_api("PUT",
        f"/values/{urllib.parse.quote(rng)}?valueInputOption={opt}",
        {"values": vals})


def rv(rng):
    r = sheets_api("GET", f"/values/{urllib.parse.quote(rng)}")
    return r.get("values", [])


def hx(h):
    h = h.lstrip("#")
    return {"red": int(h[0:2], 16)/255, "green": int(h[2:4], 16)/255, "blue": int(h[4:6], 16)/255}


def cl(i):
    r = ""
    while i >= 0:
        r = chr(65 + i % 26) + r
        i = i // 26 - 1
    return r


def kintone_api(method, path, body=None):
    """kintone REST API (env から認証情報を取得)"""
    env_path = os.path.join(os.path.dirname(__file__), ".env")
    username = password = ""
    if os.path.exists(env_path):
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line.startswith("KINTONE_USERNAME="):
                    username = line.split("=", 1)[1].strip().strip('"').strip("'")
                elif line.startswith("KINTONE_PASSWORD="):
                    password = line.split("=", 1)[1].strip().strip('"').strip("'")
    if not username or not password:
        return None

    auth = base64.b64encode(f"{username}:{password}".encode()).decode()
    url = f"https://{KINTONE_DOMAIN}{path}"
    hdr = {"X-Cybozu-Authorization": auth, "Content-Type": "application/json"}
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(url, data=data, headers=hdr, method=method)
    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        print(f"  kintone ERR {e.code}: {e.read().decode()[:300]}")
        return None


# =========================================================================
# 1. スタッフマスタ読み取り
# =========================================================================

def read_staff_master():
    rows = rv("スタッフマスタ!A1:L100")
    if len(rows) < 2:
        return []
    header = rows[0]
    store_names = ['藤沢', '伊勢佐木町', '新宿']
    staff = []
    for r in rows[1:]:
        if not r or not r[0]:
            continue
        d = {}
        for c, h in enumerate(header):
            d[str(h)] = str(r[c]) if c < len(r) else ''
        if d.get('有効フラグ') == '無効':
            continue
        assigned = [s for s in store_names if d.get(s) in ('TRUE', 'true')]
        d['対応店舗'] = ', '.join(assigned)
        staff.append(d)
    return staff


# =========================================================================
# 2. 希望収集データ ヘッダー更新
# =========================================================================

def update_wish_headers(staff, dry_run=True):
    print("\n=== 希望収集データ ヘッダー更新 ===")

    wish_staff = [s for s in staff if s.get('働き方') in ('希望シフト', '混合')]
    fixed_staff = [s for s in staff if s.get('働き方') == '固定シフト']

    nw = len(wish_staff)
    nf = len(fixed_staff)
    wish_cols = nw * 2
    total_data_cols = wish_cols + nf
    total_cols = 2 + total_data_cols

    print(f"  希望シフト: {nw}名 → {[s['氏名'] for s in wish_staff]}")
    print(f"  固定シフト: {nf}名 → {[s['氏名'] for s in fixed_staff]}")
    print(f"  合計列数: {total_cols} (A-{cl(total_cols-1)})")

    if dry_run:
        print("  [dry-run] スキップ")
        return nw + nf

    # Clean: unmerge + clear validations on Row 2-3
    info = sheets_api("GET", "?fields=sheets(properties,conditionalFormats)")
    prep = []
    prep.append({"unmergeCells": {"range": {
        "sheetId": WSID, "startRowIndex": 1, "endRowIndex": 3,
        "startColumnIndex": 2, "endColumnIndex": 50
    }}})
    bu(prep, "Unmerge")

    # Build Row 2 values
    row2 = ['', '']
    for s in wish_staff:
        row2.append(s['氏名'])
        row2.append('')
    for s in fixed_staff:
        row2.append(s['氏名'])

    # Build Row 3 values
    row3 = ['日付', '曜日']
    for _ in wish_staff:
        row3.append('出勤')
        row3.append('退勤')
    for _ in fixed_staff:
        row3.append('希望休')

    # Pad to total_cols
    while len(row2) < total_cols:
        row2.append('')
    while len(row3) < total_cols:
        row3.append('')

    ec = cl(total_cols - 1)

    # Clear Row 2-3 then write
    sheets_api("POST", f"/values/{urllib.parse.quote(f'希望収集データ!A2:{ec}3')}:clear", {})
    wv(f"希望収集データ!A2:{ec}2", [row2], "RAW")
    wv(f"希望収集データ!A3:{ec}3", [row3], "RAW")

    # Formatting
    reqs = []

    # Merge wish staff name cells (2 cols each)
    for i in range(nw):
        sc = 2 + i * 2  # 0-indexed
        reqs.append({"mergeCells": {"range": {
            "sheetId": WSID, "startRowIndex": 1, "endRowIndex": 2,
            "startColumnIndex": sc, "endColumnIndex": sc + 2
        }, "mergeType": "MERGE_ALL"}})

    # Row 2 A:B + wish section: blue
    reqs.append({"repeatCell": {"range": {
        "sheetId": WSID, "startRowIndex": 1, "endRowIndex": 2,
        "startColumnIndex": 0, "endColumnIndex": 2 + wish_cols
    }, "cell": {"userEnteredFormat": {
        "backgroundColor": hx("#D9EBF7"),
        "textFormat": {"bold": True, "fontSize": 10},
        "horizontalAlignment": "CENTER", "verticalAlignment": "MIDDLE"
    }}, "fields": "userEnteredFormat(backgroundColor,textFormat,horizontalAlignment,verticalAlignment)"}})

    # Row 2 fixed section: green
    if nf > 0:
        reqs.append({"repeatCell": {"range": {
            "sheetId": WSID, "startRowIndex": 1, "endRowIndex": 2,
            "startColumnIndex": 2 + wish_cols, "endColumnIndex": total_cols
        }, "cell": {"userEnteredFormat": {
            "backgroundColor": hx("#D9EAD3"),
            "textFormat": {"bold": True, "fontSize": 8},
            "horizontalAlignment": "CENTER", "verticalAlignment": "MIDDLE",
            "wrapStrategy": "CLIP"
        }}, "fields": "userEnteredFormat(backgroundColor,textFormat,horizontalAlignment,verticalAlignment,wrapStrategy)"}})

    # Row 3: gray sub-headers
    reqs.append({"repeatCell": {"range": {
        "sheetId": WSID, "startRowIndex": 2, "endRowIndex": 3,
        "startColumnIndex": 0, "endColumnIndex": total_cols
    }, "cell": {"userEnteredFormat": {
        "backgroundColor": hx("#E8EAED"),
        "textFormat": {"bold": True, "fontSize": 8},
        "horizontalAlignment": "CENTER", "verticalAlignment": "MIDDLE"
    }}, "fields": "userEnteredFormat(backgroundColor,textFormat,horizontalAlignment,verticalAlignment)"}})

    # Borders
    bs = {"style": "SOLID", "width": 1, "color": hx("#D9D9D9")}
    reqs.append({"updateBorders": {"range": {
        "sheetId": WSID, "startRowIndex": 1, "endRowIndex": 3,
        "startColumnIndex": 0, "endColumnIndex": total_cols
    }, "top": bs, "bottom": bs, "left": bs, "right": bs,
        "innerHorizontal": bs, "innerVertical": bs}})

    # Thick border between wish and fixed
    if nf > 0:
        tb = {"style": "SOLID_MEDIUM", "width": 2, "color": hx("#666666")}
        reqs.append({"updateBorders": {"range": {
            "sheetId": WSID, "startRowIndex": 1, "endRowIndex": 34,
            "startColumnIndex": 2 + wish_cols, "endColumnIndex": 2 + wish_cols
        }, "left": tb}})

    bu(reqs, "Format")
    print(f"  完了: 希望{nw}名 + 固定{nf}名 = {nw+nf}名")
    return nw + nf


# =========================================================================
# 3. kintone 213 同期
# =========================================================================

def sync_kintone_staff(staff, dry_run=True):
    print("\n=== kintone 213 スタッフマスタ同期 ===")
    print(f"  対象: {len(staff)}名")

    if dry_run:
        print("  [dry-run] スキップ")
        return

    # Fetch existing
    result = kintone_api("GET",
        f"/k/v1/records.json?app={KINTONE_STAFF_APP}"
        f"&query={urllib.parse.quote('order by staff_id asc limit 500')}"
        f"&totalCount=true")
    if not result:
        print("  kintone認証情報なし (.envを確認)")
        return

    exist_map = {}
    for r in result.get("records", []):
        exist_map[r["staff_id"]["value"]] = r["$id"]["value"]

    to_add = []
    to_update = []
    for s in staff:
        record = {
            "staff_id": {"value": s["staff_id"]},
            "staff_name": {"value": s["氏名"]},
            "employment_type": {"value": s.get("雇用形態", "")},
            "assigned_stores": {"value": s.get("対応店舗", "")},
            "work_style": {"value": s.get("働き方", "")},
            "position": {"value": s.get("役職", "")},
            "active": {"value": s.get("有効フラグ", "有効")},
        }
        rid = exist_map.get(s["staff_id"])
        if rid:
            to_update.append({"id": rid, "record": record})
        else:
            to_add.append(record)

    added = 0
    for i in range(0, len(to_add), 100):
        batch = to_add[i:i+100]
        kintone_api("POST", "/k/v1/records.json",
            {"app": KINTONE_STAFF_APP, "records": batch})
        added += len(batch)

    updated = 0
    for i in range(0, len(to_update), 100):
        batch = to_update[i:i+100]
        kintone_api("PUT", "/k/v1/records.json",
            {"app": KINTONE_STAFF_APP, "records": batch})
        updated += len(batch)

    print(f"  新規: {added}件 / 更新: {updated}件")


# =========================================================================
# 4. シフト出力 名前ドロップダウン更新
# =========================================================================

def update_shift_dropdowns(staff, dry_run=True):
    print("\n=== シフト出力 名前ドロップダウン更新 ===")

    active_names = [s['氏名'] for s in staff]
    print(f"  有効スタッフ: {len(active_names)}名")

    if dry_run:
        print("  [dry-run] スキップ")
        return 0

    # Name dropdown values
    name_vals = [{"userEnteredValue": ""}] + [
        {"userEnteredValue": n} for n in active_names
    ]

    reqs = []
    count = 0

    # Retail stores: name dropdown on date rows
    for store, shifts in RETAIL_COLS.items():
        for stype, ncol in shifts.items():
            for d in range(OUTPUT_DAYS):
                name_row = OUTPUT_DSTART + d * 2 - 1  # 0-indexed
                reqs.append({"setDataValidation": {"range": {
                    "sheetId": OSID,
                    "startRowIndex": name_row, "endRowIndex": name_row + 1,
                    "startColumnIndex": ncol - 1, "endColumnIndex": ncol
                }, "rule": {
                    "condition": {"type": "ONE_OF_LIST", "values": name_vals},
                    "showCustomUi": True, "strict": False
                }}})
                count += 1

    bu(reqs, "Dropdowns")

    # Update non-retail person headers in Row 3
    factory_staff = [s for s in staff if s.get('メイン店舗') == '工場']
    office_staff = [s for s in staff if s.get('メイン店舗') == '本部オフィス']

    updates = []
    for i, col in enumerate(FACTORY_PERSON_COLS):
        name = factory_staff[i]['氏名'] if i < len(factory_staff) else ''
        letter = cl(col - 1)
        updates.append((f"シフト出力!{letter}3", [[name]]))

    for i, col in enumerate(OFFICE_PERSON_COLS):
        name = office_staff[i]['氏名'] if i < len(office_staff) else ''
        letter = cl(col - 1)
        updates.append((f"シフト出力!{letter}3", [[name]]))

    for rng, vals in updates:
        wv(rng, vals, "RAW")

    print(f"  ドロップダウン: {count}箇所")
    print(f"  工場ヘッダー: {[s['氏名'] for s in factory_staff[:3]]}")
    print(f"  本部ヘッダー: {[s['氏名'] for s in office_staff[:6]]}")
    return count


# =========================================================================
# Main
# =========================================================================

def main():
    dry_run = "--no-dry-run" not in sys.argv
    if dry_run:
        print("=== ① スタッフマスタ同期 (dry-run) ===")
        print("  本番実行: python3 sync_staff_master_sheet.py --no-dry-run")
    else:
        print("=== ① スタッフマスタ同期 (本番実行) ===")

    # 1. Read staff master
    print("\n=== スタッフマスタ読み取り ===")
    staff = read_staff_master()
    print(f"  有効スタッフ: {len(staff)}名")
    for s in staff:
        print(f"    {s['staff_id']} {s['氏名']} ({s.get('働き方','')}) [{s.get('有効フラグ','')}]")

    # 2. Update wish sheet headers
    wish_count = update_wish_headers(staff, dry_run)

    # 3. Sync kintone 213
    sync_kintone_staff(staff, dry_run)

    # 4. Update shift output dropdowns
    dropdown_count = update_shift_dropdowns(staff, dry_run)

    print("\n" + "=" * 50)
    print(f"スタッフマスタ同期{'(dry-run)' if dry_run else '完了'}")
    print(f"  希望収集データ: {wish_count}名のヘッダー更新")
    if not dry_run:
        print(f"  シフト出力: {dropdown_count}箇所ドロップダウン更新")


if __name__ == "__main__":
    main()
