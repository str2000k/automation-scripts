#!/usr/bin/env python3
"""STEP 2: Create kintone apps for shift management.

Creates two apps:
1. シフト希望収集_chillaxy - Shift preference collection
2. 確定シフト_chillaxy - Confirmed shifts

Usage:
    KINTONE_PASSWORD=xxx python3 setup_kintone_apps.py
"""
import json
import base64
import urllib.request
import urllib.error
from config import KINTONE_DOMAIN, KINTONE_USERNAME, KINTONE_PASSWORD


def kintone_api(path, method="GET", body=None):
    url = f"https://{KINTONE_DOMAIN}/k/v1/{path}"
    credential = base64.b64encode(f"{KINTONE_USERNAME}:{KINTONE_PASSWORD}".encode()).decode()
    headers = {"X-Cybozu-Authorization": credential}
    if body:
        headers["Content-Type"] = "application/json"
        data = json.dumps(body).encode()
    else:
        data = None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        print(f"Error {e.code}: {e.read().decode()}")
        raise


def preview_app(name):
    """Create app in preview (draft) state."""
    result = kintone_api("preview/app.json", "POST", {"name": name})
    app_id = result["app"]
    print(f"Created preview app '{name}' -> App ID: {app_id}")
    return int(app_id)


def add_fields(app_id, fields):
    """Add fields to a preview app."""
    kintone_api("preview/app/form/fields.json", "POST", {
        "app": app_id,
        "properties": fields,
    })
    print(f"  Added {len(fields)} fields to App {app_id}")


def deploy_app(app_id):
    """Deploy (activate) a preview app."""
    kintone_api("preview/app/deploy.json", "POST", {
        "apps": [{"app": app_id}],
    })
    print(f"  Deployed App {app_id}")


def create_shift_wish_app():
    """Create: シフト希望収集_chillaxy"""
    app_id = preview_app("シフト希望収集_chillaxy")
    fields = {
        "staff_name": {
            "type": "SINGLE_LINE_TEXT",
            "code": "staff_name",
            "label": "スタッフ名",
            "required": True,
        },
        "target_period_start": {
            "type": "DATE",
            "code": "target_period_start",
            "label": "対象期間（開始）",
            "required": True,
        },
        "target_period_end": {
            "type": "DATE",
            "code": "target_period_end",
            "label": "対象期間（終了）",
            "required": True,
        },
        "desired_days_off": {
            "type": "MULTI_LINE_TEXT",
            "code": "desired_days_off",
            "label": "希望休日",
        },
        "remarks": {
            "type": "MULTI_LINE_TEXT",
            "code": "remarks",
            "label": "備考",
        },
        "input_channel": {
            "type": "DROP_DOWN",
            "code": "input_channel",
            "label": "入力経路",
            "options": {
                "Slack": {"label": "Slack", "index": "0"},
                "LINE": {"label": "LINE", "index": "1"},
                "kintone直接": {"label": "kintone直接", "index": "2"},
            },
        },
        "submitted_at": {
            "type": "DATETIME",
            "code": "submitted_at",
            "label": "提出日時",
        },
        "status": {
            "type": "DROP_DOWN",
            "code": "status",
            "label": "ステータス",
            "defaultValue": "未処理",
            "options": {
                "未処理": {"label": "未処理", "index": "0"},
                "処理済み": {"label": "処理済み", "index": "1"},
            },
        },
    }
    add_fields(app_id, fields)
    deploy_app(app_id)
    return app_id


def create_shift_confirmed_app():
    """Create: 確定シフト_chillaxy"""
    app_id = preview_app("確定シフト_chillaxy")
    fields = {
        "shift_period": {
            "type": "SINGLE_LINE_TEXT",
            "code": "shift_period",
            "label": "シフト期間",
        },
        "staff_name": {
            "type": "SINGLE_LINE_TEXT",
            "code": "staff_name",
            "label": "スタッフ名",
        },
        "working_days": {
            "type": "MULTI_LINE_TEXT",
            "code": "working_days",
            "label": "出勤日一覧",
        },
        "off_days": {
            "type": "MULTI_LINE_TEXT",
            "code": "off_days",
            "label": "休日一覧",
        },
        "total_hours": {
            "type": "NUMBER",
            "code": "total_hours",
            "label": "合計勤務時間",
        },
        "confirmed_at": {
            "type": "DATETIME",
            "code": "confirmed_at",
            "label": "確定日時",
        },
        "confirmed_by": {
            "type": "SINGLE_LINE_TEXT",
            "code": "confirmed_by",
            "label": "確定者",
        },
    }
    add_fields(app_id, fields)
    deploy_app(app_id)
    return app_id


def main():
    if not KINTONE_PASSWORD:
        print("Error: KINTONE_PASSWORD is not set.")
        print("Usage: KINTONE_PASSWORD=xxx python3 setup_kintone_apps.py")
        return

    print("=== STEP 2: kintone アプリ作成 ===\n")

    wish_app_id = create_shift_wish_app()
    print()
    confirmed_app_id = create_shift_confirmed_app()

    print(f"\n=== 完了 ===")
    print(f"シフト希望収集アプリ ID: {wish_app_id}")
    print(f"確定シフトアプリ ID:     {confirmed_app_id}")
    print(f"\n.envファイルに以下を追加してください:")
    print(f"KINTONE_SHIFT_WISH_APP_ID={wish_app_id}")
    print(f"KINTONE_SHIFT_CONFIRMED_APP_ID={confirmed_app_id}")


if __name__ == "__main__":
    main()
