#!/bin/bash
# daily-cleanup.sh — 毎日実行するMacクリーニングスクリプト
# launchdから自動実行される
#
# 動作:
#   1. Downloadsの新規ファイルを即座にDriveへコピー（バックアップ）
#   2. 30日経過したDownloadsファイルを削除（Driveにバックアップ済み）
#   3. インストーラー(DMG/PKG/EXE/APP)は即削除
#   4. キャッシュ・一時ファイルのクリーンアップ

LOG_FILE="$HOME/Library/Logs/daily-cleanup.log"
DOWNLOADS="$HOME/Downloads"
DRIVE_BASE="$HOME/Library/CloudStorage/GoogleDrive-satoru@chillaxy.jp/共有ドライブ/個人共有"
BACKUP_RECORD="$HOME/.daily-cleanup-backed-up"
DAYS_TO_KEEP=30

log() {
  echo "$(date '+%Y-%m-%d %H:%M:%S') $1" >> "$LOG_FILE"
}

# バックアップ済みファイルの記録を初期化
touch "$BACKUP_RECORD"

# ファイルがバックアップ済みか確認
is_backed_up() {
  grep -qFx "$1" "$BACKUP_RECORD" 2>/dev/null
}

# バックアップ済みとして記録
mark_backed_up() {
  echo "$1" >> "$BACKUP_RECORD"
}

# ファイル名・拡張子からカテゴリを判定
categorize() {
  local basename="$1"
  local ext="${basename##*.}"
  local ext_lower=$(echo "$ext" | tr '[:upper:]' '[:lower:]')

  # カテゴリ判定（ファイル名パターン優先）
  case "$basename" in
    *定款*|*登記*|*同意書*|*議事録*|*eLTAX*|*prefecture*)
      echo "法務登記"; return ;;
    *契約書*|*委任*|*覚書*)
      echo "契約書"; return ;;
    *確定申告*|*消費税*|*決算書*|*36協定*|*法人税*|*事業税*|*e-Tax*|*源泉徴収票*)
      echo "税務"; return ;;
    *請求書*|*Invoice*|*invoice*|*Receipt*|*receipt*|*Settlement*|*精算*|*見積*|*領収*)
      echo "経理請求書"; return ;;
    NBG*|*振込*|*torihikimeisai*|*入出金*|*ジャーナル*|*nyushukinmeisai*|*fb_data*|*sougou_fb*|*BillDetail*)
      echo "銀行明細"; return ;;
    *履歴書*|*就業規則*|*給与*|*賃金*|*シフト*|*キャリアアップ*|*雇用*|*rirekisho*|*resume*)
      echo "人事"; return ;;
    *事業計画*|*経営*|*予算*)
      echo "事業計画"; return ;;
    *recovery_code*|*client_secret*|*credential*|*secret*|*tfa_*)
      echo "機密"; return ;;
  esac

  # 拡張子で振り分け
  case "$ext_lower" in
    jpg|jpeg|png|heic|webp|avif|gif|svg|mov|mp4|wmv)
      echo "メディア" ;;
    pdf|docx|xlsx|csv|txt|pptx|doc|xls)
      echo "業務資料" ;;
    md|json|xml|elt)
      echo "業務資料" ;;
    *)
      echo "その他" ;;
  esac
}

# ファイルをDriveにコピー（バックアップ）
copy_to_drive() {
  local file="$1"
  local basename=$(basename "$file")
  local ext="${basename##*.}"
  local category=$(categorize "$basename")

  local dest_dir="$DRIVE_BASE/$category"
  mkdir -p "$dest_dir" 2>/dev/null

  # 同名ファイルが存在する場合はタイムスタンプ付きリネーム
  local dest_file="$dest_dir/$basename"
  if [ -e "$dest_file" ]; then
    local timestamp=$(date '+%Y%m%d%H%M%S')
    local name="${basename%.*}"
    if [ "$name" = "$basename" ]; then
      dest_file="$dest_dir/${basename}_${timestamp}"
    else
      dest_file="$dest_dir/${name}_${timestamp}.${ext}"
    fi
  fi

  cp -a "$file" "$dest_file" 2>/dev/null && return 0
  return 1
}

log "=== クリーニング開始 ==="

# Google Driveマウント確認
if [ ! -d "$DRIVE_BASE" ]; then
  log "ERROR: Google Driveがマウントされていません。ファイル整理をスキップ"
else

  # 1. インストーラーを即削除（Driveに保存不要）
  installer_count=0
  for f in "$DOWNLOADS"/*.dmg "$DOWNLOADS"/*.pkg "$DOWNLOADS"/*.exe; do
    [ -f "$f" ] && rm -f "$f" && installer_count=$((installer_count + 1))
  done
  for f in "$DOWNLOADS"/*.app; do
    [ -d "$f" ] && rm -rf "$f" && installer_count=$((installer_count + 1))
  done
  [ $installer_count -gt 0 ] && log "インストーラー ${installer_count}件 削除"

  # 2. 新規ファイルを即座にDriveへバックアップ（コピー、元は残す）
  backed_up=0
  for f in "$DOWNLOADS"/*; do
    [ -e "$f" ] || continue
    basename=$(basename "$f")
    [[ "$basename" == .* ]] && continue

    # インストーラー拡張子はスキップ
    ext_lower=$(echo "${basename##*.}" | tr '[:upper:]' '[:lower:]')
    case "$ext_lower" in dmg|pkg|exe) continue ;; esac
    [[ "$f" == *.app ]] && continue

    # 既にバックアップ済みならスキップ
    is_backed_up "$basename" && continue

    # Driveにコピー
    if copy_to_drive "$f"; then
      mark_backed_up "$basename"
      backed_up=$((backed_up + 1))
    fi
  done
  [ $backed_up -gt 0 ] && log "Driveへバックアップ ${backed_up}件"

  # 3. 30日経過したDownloadsファイルを削除（バックアップ済みのもののみ）
  deleted=0
  find "$DOWNLOADS" -maxdepth 1 -not -name '.*' -not -path "$DOWNLOADS" -mtime +${DAYS_TO_KEEP} 2>/dev/null | while IFS= read -r f; do
    basename=$(basename "$f")
    if is_backed_up "$basename"; then
      rm -rf "$f" 2>/dev/null && deleted=$((deleted + 1))
    else
      # バックアップされていなければ今コピーしてから削除
      if copy_to_drive "$f"; then
        mark_backed_up "$basename"
        rm -rf "$f" 2>/dev/null && deleted=$((deleted + 1))
      fi
    fi
  done
  # サブシェルのためログ用に再カウント
  old_remaining=$(find "$DOWNLOADS" -maxdepth 1 -not -name '.*' -not -path "$DOWNLOADS" -mtime +${DAYS_TO_KEEP} 2>/dev/null | wc -l | tr -d ' ')
  [ "$old_remaining" = "0" ] || log "30日超ファイル ${old_remaining}件 残存（確認要）"

fi

# 4. Chromeキャッシュ削除（Chrome未起動時のみ）
if ! pgrep -x "Google Chrome" > /dev/null 2>&1; then
  chrome_cache="$HOME/Library/Caches/Google/Chrome"
  if [ -d "$chrome_cache" ]; then
    before=$(du -sm "$chrome_cache" 2>/dev/null | cut -f1)
    find "$chrome_cache" -name "Cache" -type d -exec rm -rf {} + 2>/dev/null
    find "$chrome_cache" -name "Code Cache" -type d -exec rm -rf {} + 2>/dev/null
    find "$chrome_cache" -name "CacheStorage" -type d -exec rm -rf {} + 2>/dev/null
    find "$chrome_cache" -name "GrShaderCache" -type d -exec rm -rf {} + 2>/dev/null
    after=$(du -sm "$chrome_cache" 2>/dev/null | cut -f1)
    saved=$(( (before - after) ))
    [ $saved -gt 0 ] && log "Chromeキャッシュ ${saved}MB 削除"
  fi
else
  log "Chrome起動中のためキャッシュ削除スキップ"
fi

# 5. アプリキャッシュ削除（安全なもののみ）
caches_cleaned=0
for cache_dir in \
  "$HOME/Library/Caches/com.spotify.client" \
  "$HOME/Library/Caches/com.anthropic.claudefordesktop.ShipIt" \
  "$HOME/Library/Caches/com.todesktop.230313mzl4w4u92.ShipIt" \
  "$HOME/Library/Caches/com.openai.atlas" \
  "$HOME/Library/Caches/ms-playwright" \
  "$HOME/Library/Caches/Homebrew"; do
  if [ -d "$cache_dir" ]; then
    size=$(du -sm "$cache_dir" 2>/dev/null | cut -f1)
    rm -rf "$cache_dir" 2>/dev/null && caches_cleaned=$((caches_cleaned + size))
  fi
done
[ $caches_cleaned -gt 0 ] && log "アプリキャッシュ ${caches_cleaned}MB 削除"

# 6. システム一時ファイル
tmp_cleaned=0
for tmp_dir in "$HOME/Library/Logs/DiagnosticReports" "$TMPDIR/../com.apple.bird" "$TMPDIR/../TemporaryItems"; do
  if [ -d "$tmp_dir" ]; then
    size=$(du -sm "$tmp_dir" 2>/dev/null | cut -f1)
    rm -rf "$tmp_dir"/* 2>/dev/null && tmp_cleaned=$((tmp_cleaned + size))
  fi
done
[ $tmp_cleaned -gt 0 ] && log "一時ファイル ${tmp_cleaned}MB 削除"

# 7. バックアップ記録のクリーンアップ（Downloadsに存在しないエントリを削除）
if [ -f "$BACKUP_RECORD" ]; then
  tmp_record=$(mktemp)
  while IFS= read -r entry; do
    [ -e "$DOWNLOADS/$entry" ] && echo "$entry"
  done < "$BACKUP_RECORD" > "$tmp_record"
  mv "$tmp_record" "$BACKUP_RECORD"
fi

# 8. ログローテーション
if [ -f "$LOG_FILE" ]; then
  log_size=$(stat -f%z "$LOG_FILE" 2>/dev/null || echo 0)
  if [ "$log_size" -gt 1048576 ]; then
    tail -100 "$LOG_FILE" > "${LOG_FILE}.tmp" && mv "${LOG_FILE}.tmp" "$LOG_FILE"
    log "ログファイル切り詰め"
  fi
fi

log "=== クリーニング完了 ==="
