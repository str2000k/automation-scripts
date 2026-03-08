#!/usr/bin/env python3
"""Sync staff master from Google Spreadsheet to local JSON cache.

This reads the 'スタッフマスタ' sheet via Google Sheets API and writes
staff_master_cache.json for use by the Slack bot.

Requires google-auth and google-api-python-client:
    pip install google-auth google-api-python-client
"""
import json
import os

SPREADSHEET_ID = os.environ.get("SHIFT_SPREADSHEET_ID", "")
SHEET_NAME = "スタッフマスタ"
TOKEN_FILE = os.path.expanduser("~/.google_workspace_mcp/chillaxy/satoru@chillaxy.jp.json")
CACHE_FILE = os.path.join(os.path.dirname(__file__), "staff_master_cache.json")


def get_credentials():
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request

    with open(TOKEN_FILE) as f:
        token_data = json.load(f)

    creds = Credentials(
        token=token_data.get("token"),
        refresh_token=token_data.get("refresh_token"),
        token_uri="https://oauth2.googleapis.com/token",
        client_id=token_data.get("client_id"),
        client_secret=token_data.get("client_secret"),
    )
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
    return creds


def read_sheet():
    from googleapiclient.discovery import build

    creds = get_credentials()
    service = build("sheets", "v4", credentials=creds)
    result = service.spreadsheets().values().get(
        spreadsheetId=SPREADSHEET_ID,
        range=f"{SHEET_NAME}!A:I",
    ).execute()
    return result.get("values", [])


def main():
    if not SPREADSHEET_ID:
        print("Error: SHIFT_SPREADSHEET_ID not set.")
        return

    rows = read_sheet()
    if len(rows) < 2:
        print("No data in staff master sheet.")
        return

    headers = rows[0]
    # Expected: staff_id, 氏名, 雇用形態, 最大労働時間/週, 強制出勤日, 役割・スキル, Slack ID, LINE UID, 有効フラグ
    staff = []
    for row in rows[1:]:
        while len(row) < len(headers):
            row.append("")
        entry = {
            "staff_id": row[0],
            "name": row[1],
            "employment_type": row[2],
            "max_hours": float(row[3]) if row[3] else 40,
            "forced_days": row[4],
            "skills": row[5],
            "slack_id": row[6],
            "line_uid": row[7],
            "active": row[8].upper() == "TRUE" if row[8] else True,
        }
        staff.append(entry)

    with open(CACHE_FILE, "w") as f:
        json.dump(staff, f, ensure_ascii=False, indent=2)

    print(f"Synced {len(staff)} staff entries to {CACHE_FILE}")


if __name__ == "__main__":
    main()
