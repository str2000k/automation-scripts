# シフト管理システム - ガードレール & ルール

## 1. 正本管理

- **確定シフトの正本**: kintone ID=212 のみ
- **スプレッドシート**: レビュー・承認UIとして使用。正本ではない
- **承認後のデータ**: immutableとして扱う。変更は新バージョンとして発番

## 2. 主キー原則

- **staff_id** (S001形式) を全処理の不変主キーとする
- staff_nameを主キーとして使用禁止（表示名としてのみ使用可）
- Slack ID / LINE UID / Calendar / kintoneレコードは全て staff_id で紐づける

## 3. 必須概念

以下のIDは全処理で一貫して使用すること:

| ID | 形式 | 説明 |
|---|---|---|
| staff_id | S001 | スタッフ不変ID |
| period_id | 2026-03-09_2026-03-22 | シフト期間ID (start_end) |
| schedule_version | 2026-03-09_v1 | 承認済みバージョン |
| assignment_id | S001_2026-03-09 | スタッフ×日付の割当ID |

## 4. 書き込み前チェック (必須)

全スクリプトは書き込み前に以下を検証すること:

| チェック項目 | 許可値 |
|---|---|
| SpreadsheetID | `1JlyWngnuha1IHQLMGs5bzTnjct8s7eY4Z-bW0YHIlmU` のみ |
| kintone App ID | 211 (希望収集) または 212 (確定シフト) のみ |
| Google Workspace MCP | gw-chillaxy のみ |
| dry-run | デフォルト True。--no-dry-run で本番実行 |

## 5. 禁止事項

- gw-master / gw-goodshit / gw-blonde / gw-fujisawa / gw-graytattoo / gw-mtrx への書き込み禁止
- .env ファイルの内容をログや出力に含めない
- main branch で dry-run 未確認の本番書き込み禁止
- APIキー・トークンのハードコード禁止
- staff_name を主キー・結合キーとして使用禁止

## 6. Claude (LLM) の役割

- **候補生成のみ**: シフト案の生成は LLM が行うが、制約検証は Python 側で必ず実行
- **定期実行基盤として使わない**: cron / GAS トリガー等で自動化
- **shift_validator.py**: hardルール全件検証。違反時は再生成 (最大3回) または human review

## 7. dry-run 原則

- 全スクリプトは `--dry-run` (デフォルト) / `--no-dry-run` (本番) を持つ
- 実行前に対象ID・アカウント・書き込み先をログ出力して確認
- dry-run 時は read のみ実行し write は全てスキップ

## 8. 冪等性 & sync_outbox

- 同じデータを2回処理しても重複登録しない
- kintone: period_id + staff_id で既存レコードを検索し upsert
- Calendar: [shift-sync] タグで既存イベントを検索し delete → re-create
- **sync_outbox**: 送信先ごとに pending/sent/failed/partial_failed を管理
  - ファイル: sync_outbox.json (git管理外)
  - 送信先: slack / line / calendar / kintone / mf_kintai
  - 承認時に schedule_version (例: 2026-04-01_v1) を発番
  - 各スクリプトは outbox の should_run() をチェックしてから実行
  - 失敗時は failed として記録し、再実行で自動リトライ
  - `python3 run_post_approval.py [version]` で全ステップ一括実行

## 9. エラーハンドリング

- 全処理に try-except 実装
- エラー時は Slack 管理者チャンネル (#shift-management) に通知
- 通知内容: 発生箇所 / エラー内容 / 日時

## 10. ファイル構成

```
scripts/shift-management/
├── CLAUDE.md              # このファイル (ガードレール)
├── config.py              # 環境変数管理
├── .env / .env.example    # 認証情報 (git管理外)
├── slack_shift_bot.py     # Slack希望収集
├── line_bot.py            # LINE希望収集
├── sync_staff_master.py   # スタッフマスタ同期
├── generate_shift.py      # AI シフト生成
├── shift_validator.py     # hardルール検証 (STEP C)
├── sync_outbox.py         # 送信先ごとの状態管理 (STEP D)
├── run_post_approval.py   # 承認後一括実行 (STEP D)
├── gas_approval.js        # GAS承認ボタン + schedule_version発番
├── notify_shift.py        # Slack/LINE配信 (outbox連携)
├── calendar_sync.py       # Google Calendar同期 (outbox連携)
├── kintone_shift_register.py  # kintone確定登録 (outbox連携)
├── mf_kintai_sync.py      # MFクラウド勤怠同期 (STEP E)
├── reminder.py            # シフト開始リマインド (STEP F)
├── test_integration.py    # 統合テスト
└── test_e2e.py            # E2Eテスト (STEP G)
```
