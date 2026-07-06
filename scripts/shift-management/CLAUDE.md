# シフト勤怠管理システム v2 - ガードレール & ルール

**2026-07-03 全面刷新**: kintone/Cloud Run/Python パイプラインを全廃し、
**Slack（入力・通知） + Googleスプレッドシート（正本） + Googleカレンダー（共有）** の3点構成に再構築。
頭脳は GAS 1本（`gas/Code.js`）。旧構成は `archive/` に退避（git履歴あり）。

## 1. 構成（v2）

| 役割 | 実体 |
|---|---|
| 正本（データ） | スプレッドシート「シフト勤怠管理」`1JlyWngnuha1IHQLMGs5bzTnjct8s7eY4Z-bW0YHIlmU`（旧名「シフト労務管理」2026-07-06リネーム・ID不変） |
| 頭脳 | GAS「シフト管理GAS」Script ID `1RWVWISiDyypxfSCviSOovv2vDbdmIgfwKx25YGASsjVCxd6zIScZVewf`（このリポジトリの `gas/Code.js` が正） |
| Slack受け口 | GAS Web App（doPost）。Slackアプリ「勤怠管理」A0AG9QRBLGH の Interactivity Request URL が向く |
| AI生成 | Gemini `gemini-2.5-flash`（専用GCPプロジェクト `shift-mgmt-gemini-26`・無料枠・キーは Script Properties `GEMINI_API_KEY`） |
| 共有 | Googleカレンダー「全体シフト」`[shift-sync]` タグ同期 |
| 定期実行 | GASトリガーのみ（onEditTrigger / dailyShiftReminder 毎朝 / attendanceCron 10分毎） |

**廃止済み**: kintone 211/212/213（全件バックアップ: Gドライブ `シフト管理_kintoneバックアップ_20260703/`）、
Cloud Run `shift-bolt-server`、launchd 4本、GHA shift-*.yml 3本、LINE/LIFF/SmartHR/MF勤怠連携。
※日報（`daily_report_slack.py` + `daily-report-slack.yml`、kintone 110）は**別機能として現役**。

## 2. シート構成（タブはユーザーがリネームすることがある → GASは gid で解決）

| 論理名（コード内定数） | 現在の表示名 | gid | 用途 |
|---|---|---|---|
| シフト出力 | シフト作成 | 1533022256 | 作成・確定UI（A1=年/A2=月セレクター、2行=1日、確定/変更チェック） |
| 希望収集データ | 希望シフト | 1764958489 | 希望のグリッド表示・手入力（A1=年/A2=月） |
| スタッフマスタ | S | 1657902443 | スタッフ正本（staff_id/氏名/働き方/店舗ブール列/Slack ID…） |
| 店舗マスタ | T | 375450408 | 店舗正本（種別/勤務開始・終了/最低必要人数） |
| 共通ルールマスタ | R | 456922734 | 自由記述ルール（AIプロンプトに注入） |
| 個人シフト | 個人シフト | 363542595 | 個人ビュー（A1=年/A2=月/C2=名前、全てドロップダウン） |
| シフトデータ | （非表示） | 712324603 | **確定シフトの正本**（1行=staff×日付×店舗） |
| 希望データ | （非表示） | 1404449834 | **希望の正本**（1行=staff×日付。Slack/手編集の双方から同期） |
| 勤怠 | （非表示） | 2007718750 | 出勤確認・遅刻記録（Slackボタン応答が自動追記） |

- 非表示3タブは正本。**削除禁止**。GASが再作成時も自動で非表示にする。
- gid⇄論理名のマップは `Code.js` の `SN_GID_MAP_`。**タブ追加・作り直し時はここを更新**。

## 3. 主キー原則（変更なし）

- **staff_id**（S001形式）が全処理の不変主キー。staff_name は表示用。
- 確定シフトの一意キー: `staff_id + shift_date + store`。
- 希望の一意キー: `staff_id + date`（upsert）。

## 4. Slack 連携

- アプリ: 「勤怠管理」A0AG9QRBLGH（bot user `claude_mcp`・旧名「シフト管理」）/ チャンネル `#3002-直営共有-勤怠管理` C0AKBJ1LTV2（旧 #shift-management。2026-07-06リネーム・ID不変）
- Interactivity Request URL: GAS Web App `.../exec?token=<WEBHOOK_TOKEN>`（GASは署名検証不可のためURLトークン方式・p10と同方式）
- **Web App のコード変更はデプロイ更新が必要**（同一URL維持のため deployments.update で version 差し替え。新規 create_deployment するとURLが変わりSlack再設定になる）
- 現行デプロイID: `AKfycbxE4CbtJSeI3z36QsoawujDDhbqzpJH1iSqAceWPwpcfpfqc6pzB83HIUYIBvjvRVjg`
- doPost 管理アクション（token必須）: `&probe=1` 疎通 / `&admin=reload&year=&month=` 再描画 / `&admin=personal` 個人シフト再描画 / `&admin=staffsync` ヘッダ再構築 / `&admin=setprop`（許可キーのみ、POSTボディで） / `&admin=calsync&date=YYYY-MM-DD` カレンダー同期の実地テスト（`&debug=1`で生レスポンス） / `&admin=caldel&date=` 指定日の[shift-sync]イベント削除 / `&admin=triggers` トリガー復旧（3種を未設定なら作成）
- **Calendar Advanced Service 必須**（2026-07-06修正）: `appsscript.json` の `enabledAdvancedServices` に calendar v3。これがないとスクリプトのGCPプロジェクトで Calendar REST API が無効のまま → `syncToCalendar_` の UrlFetch が全件403で `created=0` になる（例外は出ない）

## 5. 業務フロー

1. **希望収集**: 毎月1日/15日に dailyShiftReminder が対象期間のDM送信（✅出勤/🙏休み希望ボタン）→ doPost → 希望データ＋グリッド即反映。管理者はグリッド直接編集も可（onEditで正本に同期）
2. **生成**: メニュー②AIシフト生成 → 期間ダイアログ → Gemini生成（検証NG時は最大3回再生成）→ シフト出力に書き込み
3. **確定**: 出力シートで修正 → A列「確定」チェック → ③Googleカレンダー反映 → シフトデータ（正本）＋カレンダー同期
4. **勤怠（新機能）**: attendanceCron が出勤60分前に #3002-直営共有-勤怠管理 へメンション付き出勤確認を投稿（🟢通常出勤/🕐遅刻→5分刻みドロップダウンで分数選択。本人と管理者のみ操作可・2026-07-06にDM方式から変更）→ 勤怠タブに記録。30分前に未応答再通知、出勤時刻超過で同チャンネルにアラート。タイミングは Script Properties `ATTEND_NOTIFY_MIN`/`ATTEND_RENOTIFY_MIN`（分）

## 6. Script Properties（必須キー）

`GEMINI_API_KEY` / `SLACK_BOT_TOKEN` / `SLACK_SHIFT_CHANNEL` / `SHIFT_CALENDAR_ID` / `WEBHOOK_TOKEN`
（旧 KINTONE_* / ANTHROPIC_API_KEY は残存していても未使用）

## 7. 開発ルール

- **コードの正はこのリポジトリの `gas/Code.js`**。リモート編集したら必ずローカルへ反映してコミット
- push は Apps Script API（`~/.google_workspace_mcp/chillaxy/satoru@chillaxy.jp.json` のOAuthでアクセストークン取得 → projects.updateContent）。clasp はトークン失効しがち
- デプロイ更新: versions.create → deployments.update（**同一デプロイID維持**）
- シートのA1記法での名前参照はMCP経由だと日本語名で失敗することがある → **gid指定のCSV export / batchUpdate を使う**
- 全処理 try-except、エラーは `slackError_` で #3002-直営共有-勤怠管理 へ
- 破壊的変更前にバックアップ。シートデータは gid指定エクスポートで退避

## 8. 既知の注意点

- Gemini 無料枠: flash は 20リクエスト/日/プロジェクト。専用プロジェクト分離済みだが、生成リトライで消費するので注意
- Gemini出力はJSON mode + maxOutputTokens 65536 + thinkingBudget 4096。途中切れは finishReason で検出
- 希望収集グリッドの列レイアウトはスタッフ構成で変動（月初にヘッダ確定、期中は固定運用）
- メモ列（シフト出力AF）は結合セルがあるため週末色はメモ列手前まで
- **AIシフト生成の精度調整は未完（2026-07-03時点・後回し指示）**: 営業時間・希望の反映を改善中

## 9. 残タスク（2026-07-06更新）

- [x] launchd 4本の停止（2026-07-06 ユーザー実行。bootout + plist を `~/.Trash/launchagents-shift-backup/` に退避・復元可能）
- [x] Cloud Run `shift-bolt-server` 削除済み（2026-07-06。Slack Event Subscriptions Off →削除→ airregi-webhook のみ残存を確認。
      設定バックアップ: `archive/cloud-run/shift-bolt-server_backup_20260706.yaml`）
- [x] kintone 211/212/213 の運用停止（2026-07-06: 「【退役】」リネーム+説明にバックアップ所在を記載・deploy済み。
      アプリ自体の削除は管理画面GUIのみ可 → 任意・要ユーザー判断）
- [ ] AIシフト生成の精度検証・調整（後回し中）
- [x] ④確定→カレンダー同期の実地検証（2026-07-06: **Calendar API無効の潜在バグを発見・修正**（§4参照）。
      admin=calsync でイベント作成→内容確認→caldel 掃除まで一次情報で確認）
- [x] 勤怠DMフローの実地検証（2026-07-06: テストシフト行→cron実発火→DM実受信→勤怠タブ記録→
      att_okボタン応答記録まで全経路確認。テストデータは掃除済み）
- [x] メニュー簡素化（2026-07-06: 「シフト勤怠管理」①マスタ反映/②AIシフト生成/③Googleカレンダー反映 の3項目のみに（月読込=onEdit自動、トリガー=admin=triggersで復旧可のため除外）。
      外した loadMonthData/fetchWishData/debugProperties/renderPersonalShift_/setupTrigger はコードに残置・admin アクションで代替可）
- [ ] ヘッダ保護（任意）
