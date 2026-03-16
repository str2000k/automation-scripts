#!/usr/bin/env python3
"""One-time spreadsheet setup: data validation, formatting, etc.

Usage:
    python3 setup_spreadsheet.py              # dry-run
    python3 setup_spreadsheet.py --no-dry-run # execute
"""
import json
import os
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timedelta

from config import SHIFT_SPREADSHEET_ID

# Sheet IDs (from spreadsheet info)
SHEET_STAFF_MASTER = 1657902443
SHEET_STORE_MASTER = 375450408
SHEET_WISH_DATA = 1764958489
SHEET_OUTPUT = 1533022256


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


def sheets_batch_update(requests_body):
    cred_path = _find_google_credential()
    if not cred_path:
        raise RuntimeError("No Google OAuth credential found")
    token = _get_access_token(cred_path)
    url = f"https://sheets.googleapis.com/v4/spreadsheets/{SHIFT_SPREADSHEET_ID}:batchUpdate"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    body = json.dumps({"requests": requests_body})
    req = urllib.request.Request(url, data=body.encode(), headers=headers, method="POST")
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read())


def sheets_values_update(range_name, values):
    """Write values to a range using values.update API."""
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


def setup_store_checkboxes(dry_run=True):
    """Set up store checkboxes in I-M columns.

    BOOLEAN data validation on I2:M100 (renders as checkboxes).
    対応店舗 is computed at read time by code, no TEXTJOIN column needed.
    """
    print("=== 対応店舗チェックボックス設定 ===")
    print(f"  Spreadsheet: {SHIFT_SPREADSHEET_ID}")
    print(f"  チェックボックス: I2:M100 (藤沢/伊勢佐木町/新宿/工場/本部オフィス)")
    print(f"  dry-run: {dry_run}\n")

    requests = [
        {
            "setDataValidation": {
                "range": {
                    "sheetId": SHEET_STAFF_MASTER,
                    "startRowIndex": 1,
                    "endRowIndex": 100,
                    "startColumnIndex": 8,   # Column I (0-indexed)
                    "endColumnIndex": 13,    # Column M+1
                },
                "rule": {
                    "condition": {
                        "type": "BOOLEAN",
                    },
                    "showCustomUi": True,
                    "strict": True,
                }
            }
        },
    ]

    if dry_run:
        print("[dry-run] Would apply:")
        print("  Set BOOLEAN validation on L2:P100 (checkboxes)")
        return

    result = sheets_batch_update(requests)
    print(f"  Validation set. Replies: {len(result.get('replies', []))}")
    print("  Done!")


def main():
    dry_run = "--no-dry-run" not in sys.argv
    if "--help" in sys.argv:
        print(__doc__)
        return
    setup_store_checkboxes(dry_run=dry_run)


if __name__ == "__main__":
    main()
