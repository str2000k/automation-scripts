#!/usr/bin/env python3
"""Kintone日報報告 → Slack Block Kit投稿スクリプト

前日の日報をkintone App 110から取得し、Block Kitフォーマットで
Slack #3003-直営共有-日報報告 に投稿する。

1. サマリー（全店舗の売上比較テーブル）をメインメッセージとして投稿
2. 各店舗ごとに個別メッセージ（売上テーブル）を投稿
3. 各店舗メッセージのスレッドに詳細（接客・在庫・共有事項）を投稿

Usage:
    python3 daily_report_slack.py                    # 前日の日報を投稿 (dry-run)
    python3 daily_report_slack.py --no-dry-run       # 本番実行
    python3 daily_report_slack.py --date 2026-03-17  # 指定日の日報を投稿
"""

import argparse
import base64
import json
import os
import urllib.request
import urllib.parse
from datetime import datetime, timedelta
from pathlib import Path

# Load .env
_env_path = Path(__file__).parent / ".env"
if _env_path.exists():
    for line in _env_path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, val = line.partition("=")
            os.environ.setdefault(key.strip(), val.strip())

KINTONE_DOMAIN = os.environ.get("KINTONE_DOMAIN", "ny76p.cybozu.com")
KINTONE_USERNAME = os.environ.get("KINTONE_USERNAME", "")
KINTONE_PASSWORD = os.environ.get("KINTONE_PASSWORD", "")
SLACK_BOT_TOKEN = os.environ.get("DAILY_REPORT_SLACK_TOKEN", "")
DAILY_REPORT_CHANNEL = os.environ.get("DAILY_REPORT_CHANNEL", "C082B8480V6")
KINTONE_DAILY_REPORT_APP_ID = 110

WEATHER_EMOJI = {"晴れ": "☀️", "曇り": "☁️", "雨": "🌧️", "雪": "❄️"}
DAYS_JP = ["月", "火", "水", "木", "金", "土", "日"]

STORE_ORDER = [
    "チラクシー新宿店",
    "チラクシー麻布店",
    "チラクシー伊勢佐木町店",
    "グッチル藤沢本店",
    "グッチル百人町店",
]

STORE_SHORT = {
    "チラクシー新宿店": "新宿",
    "チラクシー麻布店": "麻布",
    "チラクシー伊勢佐木町店": "伊勢佐木町",
    "グッチル藤沢本店": "藤沢",
    "グッチル百人町店": "百人町",
}


def kintone_get_records(app_id, query, fields=None):
    """kintone REST API でレコード取得."""
    params = {"app": str(app_id), "query": query}
    if fields:
        for i, f in enumerate(fields):
            params[f"fields[{i}]"] = f
    qs = urllib.parse.urlencode(params)
    url = f"https://{KINTONE_DOMAIN}/k/v1/records.json?{qs}"
    cred = base64.b64encode(f"{KINTONE_USERNAME}:{KINTONE_PASSWORD}".encode()).decode()
    req = urllib.request.Request(url, headers={"X-Cybozu-Authorization": cred})
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read().decode()).get("records", [])


def fv(record, field, default=""):
    """Get field value from kintone record."""
    return record.get(field, {}).get("value", default) or default


def is_empty(text):
    """内容が実質空か判定."""
    if not text:
        return True
    lines = [l.strip() for l in text.strip().split("\n") if l.strip() and l.strip() != "・"]
    return len(lines) == 0


def clean_content(text):
    """空の「・」行を除去してクリーンアップ."""
    if not text:
        return ""
    cleaned = "\n".join(
        l for l in text.strip().split("\n")
        if l.strip() and l.strip() != "・"
    )
    if len(cleaned) > 2800:
        cleaned = cleaned[:2800] + "…"
    return cleaned


def fmt_yen(val):
    try:
        return f"¥{int(float(val)):,}"
    except (ValueError, TypeError):
        return "—"


def fmt_pct(val):
    try:
        pct = float(val)
        icon = "✅" if pct >= 100 else ("⚠️" if pct >= 90 else "🔻")
        return f"{pct:.1f}% {icon}"
    except (ValueError, TypeError):
        return "—"


def to_int(val):
    try:
        return int(float(val))
    except (ValueError, TypeError):
        return 0


def fetch_reports(target_date):
    """指定日の日報をkintoneから取得."""
    query = f'日付 = "{target_date}" and 店舗名 not in ("見本入力") order by 店舗名 asc'
    fields = [
        "店舗名", "日付", "天気１", "担当者名１", "担当者名２", "担当者３",
        "売上目標", "売上", "目標達成率", "会計数", "客単価",
        "在庫切れ", "入荷予定商品", "その他共有事項",
        "良かった接客", "悪かった接客", "雑談", "お店のためにしたこと",
    ]
    return kintone_get_records(KINTONE_DAILY_REPORT_APP_ID, query, fields)


def sort_by_store(records):
    def key(r):
        try:
            return STORE_ORDER.index(fv(r, "店舗名"))
        except ValueError:
            return 999
    return sorted(records, key=key)


def build_summary_blocks(records, target_date):
    """サマリーメッセージ: 全店舗の売上比較テーブル."""
    dt = datetime.strptime(target_date, "%Y-%m-%d")
    dow = DAYS_JP[dt.weekday()]

    total_sales = total_target = total_tx = 0
    store_rows = []

    for r in records:
        short = STORE_SHORT.get(fv(r, "店舗名"), fv(r, "店舗名"))
        sales = to_int(fv(r, "売上", "0"))
        target = to_int(fv(r, "売上目標", "0"))
        tx = to_int(fv(r, "会計数", "0"))
        unit_price = to_int(fv(r, "客単価", "0"))
        total_sales += sales
        total_target += target
        total_tx += tx
        try:
            pct = float(fv(r, "目標達成率", "0"))
        except (ValueError, TypeError):
            pct = 0.0
        store_rows.append((short, sales, target, pct, tx, unit_price))

    total_pct = total_sales / total_target * 100 if total_target else 0
    avg_unit = total_sales // total_tx if total_tx else 0

    # mrkdwn リスト形式で各店舗を表示
    summary_lines = []
    for short, sales, target, pct, tx, unit_price in store_rows:
        icon = "✅" if pct >= 100 else ("⚠️" if pct >= 90 else "🔻")
        summary_lines.append(
            f"*{short}*　{icon} *{pct:.1f}%*\n"
            f"　　💰 売上 *¥{sales:,}*　／　🎯 目標 *¥{target:,}*\n"
            f"　　🧾 会計 *{tx}件*　／　💵 客単価 *¥{unit_price:,}*"
        )

    total_icon = "✅" if total_pct >= 100 else ("⚠️" if total_pct >= 90 else "🔻")
    total_line = (
        f"*📊 合計*　{total_icon} *{total_pct:.1f}%*\n"
        f"　　💰 売上 *¥{total_sales:,}*　／　🎯 目標 *¥{total_target:,}*\n"
        f"　　🧾 会計 *{total_tx}件*　／　💵 客単価 *¥{avg_unit:,}*"
    )

    blocks = [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": f"📋 日報報告  {dt.month}/{dt.day}（{dow}）",
                "emoji": True,
            },
        },
        {"type": "divider"},
    ]

    for line in summary_lines:
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": line},
        })

    blocks.append({"type": "divider"})
    blocks.append({
        "type": "section",
        "text": {"type": "mrkdwn", "text": total_line},
    })

    return blocks


def build_store_header_blocks(record, target_date):
    """各店舗のメインメッセージ: 売上テーブル + 担当者."""
    store = fv(record, "店舗名")
    weather = WEATHER_EMOJI.get(fv(record, "天気１"), "")
    dt = datetime.strptime(target_date, "%Y-%m-%d")
    dow = DAYS_JP[dt.weekday()]

    sales = fv(record, "売上", "0")
    target = fv(record, "売上目標", "0")
    achievement = fv(record, "目標達成率", "0")
    tx = fv(record, "会計数", "0")
    unit_price = fv(record, "客単価", "0")
    staff = " / ".join(s for s in [fv(record, "担当者名１"), fv(record, "担当者名２"), fv(record, "担当者３")] if s)

    # 2カラムfields形式で売上情報を表示
    blocks = [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": f"🏪 {store}  {weather}  {dt.month}/{dt.day}（{dow}）",
                "emoji": True,
            },
        },
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*💰 売上*\n{fmt_yen(sales)}"},
                {"type": "mrkdwn", "text": f"*🎯 目標*\n{fmt_yen(target)}"},
                {"type": "mrkdwn", "text": f"*📈 達成率*\n{fmt_pct(achievement)}"},
                {"type": "mrkdwn", "text": f"*🧾 会計数*\n{tx}件"},
                {"type": "mrkdwn", "text": f"*💵 客単価*\n{fmt_yen(unit_price)}"},
                {"type": "mrkdwn", "text": f"*👤 担当*\n{staff}"},
            ],
        },
    ]

    # 在庫切れ・入荷予定・共有事項をプレビュー表示（内容がある場合のみ）
    preview_items = []
    if not is_empty(fv(record, "在庫切れ")):
        content = clean_content(fv(record, "在庫切れ"))
        first_line = content.split("\n")[0][:50]
        preview_items.append(f"📦 在庫切れ: {first_line}")
    if not is_empty(fv(record, "その他共有事項")):
        content = clean_content(fv(record, "その他共有事項"))
        first_line = content.split("\n")[0][:50]
        preview_items.append(f"📝 共有: {first_line}")

    if preview_items:
        blocks.append({
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": "　｜　".join(preview_items)}],
        })

    blocks.append({
        "type": "context",
        "elements": [{"type": "mrkdwn", "text": "↩️ 詳細はスレッドを確認"}],
    })

    return blocks


def build_store_detail_blocks(record):
    """各店舗の詳細（スレッド返信用）のBlock Kit."""
    store = fv(record, "店舗名")
    weather = WEATHER_EMOJI.get(fv(record, "天気１"), "")

    blocks = [{
        "type": "header",
        "text": {"type": "plain_text", "text": f"📝 {store} - 詳細レポート", "emoji": True},
    }]

    sections = [
        ("📦", "在庫切れ", fv(record, "在庫切れ")),
        ("📬", "入荷予定", fv(record, "入荷予定商品")),
        ("📝", "共有事項", fv(record, "その他共有事項")),
        ("👍", "良かった接客", fv(record, "良かった接客")),
        ("💡", "改善点", fv(record, "悪かった接客")),
        ("💬", "お客様の声", fv(record, "雑談")),
        ("🧹", "お店のために", fv(record, "お店のためにしたこと")),
    ]

    for emoji, title, content in sections:
        if not is_empty(content):
            cleaned = clean_content(content)
            blocks.append({"type": "divider"})
            blocks.append({
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"*{emoji} {title}*\n{cleaned}"},
            })

    if len(blocks) == 1:
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": "_（記入項目なし）_"},
        })

    return blocks


def slack_post(channel, text, blocks=None, thread_ts=None):
    """Slack chat.postMessage API."""
    payload = {
        "channel": channel,
        "text": text,
        "unfurl_links": False,
        "unfurl_media": False,
    }
    if blocks:
        payload["blocks"] = blocks
    if thread_ts:
        payload["thread_ts"] = thread_ts

    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        "https://slack.com/api/chat.postMessage",
        data=data,
        headers={
            "Authorization": f"Bearer {SLACK_BOT_TOKEN}",
            "Content-Type": "application/json; charset=utf-8",
        },
        method="POST",
    )
    with urllib.request.urlopen(req) as resp:
        result = json.loads(resp.read().decode())
        if not result.get("ok"):
            raise RuntimeError(f"Slack API error: {result.get('error')}")
        return result


def main():
    parser = argparse.ArgumentParser(description="日報報告 Slack投稿")
    parser.add_argument("--date", help="対象日 (YYYY-MM-DD)。デフォルトは前日")
    parser.add_argument("--dry-run", action="store_true", default=True)
    parser.add_argument("--no-dry-run", dest="dry_run", action="store_false")
    args = parser.parse_args()

    target_date = args.date or (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")

    print(f"📋 日報報告取得: {target_date}")
    print(f"📢 投稿先: {DAILY_REPORT_CHANNEL}")
    print(f"🔧 モード: {'dry-run' if args.dry_run else '本番'}")

    records = fetch_reports(target_date)
    if not records:
        print(f"⚠️  {target_date} の日報データが見つかりません")
        return

    sorted_records = sort_by_store(records)
    print(f"✅ {len(sorted_records)}件の日報を取得")

    summary_blocks = build_summary_blocks(sorted_records, target_date)

    if args.dry_run:
        print("\n--- サマリーメッセージ ---")
        print(json.dumps(summary_blocks, ensure_ascii=False, indent=2))
        for r in sorted_records:
            header = build_store_header_blocks(r, target_date)
            detail = build_store_detail_blocks(r)
            print(f"\n--- {fv(r, '店舗名')} ヘッダー ---")
            print(json.dumps(header, ensure_ascii=False, indent=2))
            print(f"\n--- {fv(r, '店舗名')} スレッド ---")
            print(json.dumps(detail, ensure_ascii=False, indent=2))
        print("\n✅ dry-run完了。本番実行は --no-dry-run を指定")
        return

    # 1. サマリー投稿（全店舗比較テーブル）
    dt = datetime.strptime(target_date, "%Y-%m-%d")
    dow = DAYS_JP[dt.weekday()]
    fallback = f"📋 日報報告 {dt.month}/{dt.day}（{dow}）- {len(sorted_records)}店舗"
    slack_post(DAILY_REPORT_CHANNEL, fallback, summary_blocks)
    print(f"✅ サマリー投稿完了")

    # 2. 各店舗ごとにメッセージ + スレッドで詳細
    for r in sorted_records:
        store = fv(r, "店舗名")

        # 店舗ヘッダー（トップレベルメッセージ）
        header_blocks = build_store_header_blocks(r, target_date)
        result = slack_post(DAILY_REPORT_CHANNEL, f"🏪 {store}", header_blocks)
        store_ts = result["ts"]

        # 詳細（スレッド返信）
        detail_blocks = build_store_detail_blocks(r)
        slack_post(DAILY_REPORT_CHANNEL, f"📝 {store} 詳細", detail_blocks, thread_ts=store_ts)
        print(f"  ✅ {store}")

    print(f"\n🎉 全{len(sorted_records)}店舗の日報投稿完了")


if __name__ == "__main__":
    main()
