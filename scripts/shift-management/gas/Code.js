/**
 * シフト勤怠管理システム v2 - Google Apps Script
 *
 * スプレッドシート「シフト勤怠管理」: 1JlyWngnuha1IHQLMGs5bzTnjct8s7eY4Z-bW0YHIlmU
 *
 * メニュー「シフト勤怠管理」(2026-07-06 簡素化):
 *   ① スタッフマスタ同期: スタッフマスタ → 各シートのヘッダー/ドロップダウン再構築
 *   ② AIシフト生成: Gemini → シフト出力シート
 *   ③ 確定シフト反映: チェック済み行 → シフトデータ(正本) + Google Calendar
 *   ※月データ読込は年月セレクター変更のonEditで自動実行 (手動: admin=reload)
 *   ※トリガー復旧は admin=triggers (onEditは主要関数のensureTrigger_でも自己修復)
 *
 * シフト出力 横型レイアウト (動的列数):
 *   Row 1: A1=年セレクター + 店舗名ヘッダー (藤沢/伊勢佐木町/新宿/工場/EC/本部オフィス/メモ)
 *   Row 2: A2=月セレクター + シフト種別 (営業: 早番/遅番/※予備) / 担当者名 (非営業)
 *   Row 3: カラムヘッダー (確定/変更/日付/出勤/退勤...)
 *   Row 4+: データ (2行/日 × 31日 = 62行)
 *     - 日付行(名前行): 営業=名前プルダウン, 非営業=出勤/公休プルダウン
 *     - 曜日行(時間行): 出勤/退勤の時間プルダウン (6:00-25:00)
 *
 *   Columns (1-indexed):
 *     A(1)=確定  B(2)=変更  C(3)=日付
 *     D-I(4-9):    藤沢      (早番[出,退], 遅番[出,退], ※予備[出,退])
 *     J-O(10-15):  伊勢佐木町 (早番[出,退], 遅番[出,退], ※予備[出,退])
 *     P-U(16-21):  新宿      (早番[出,退], ※予備[出,退], ※予備[出,退])
 *     V+:          非営業店舗  (動的: スタッフマスタのメイン店舗に基づく)
 *     末尾:        メモ
 *
 * installable onEdit trigger:
 *   onEditTrigger(e) → 年月変更時にkintoneからデータ自動読込
 *   設定: Apps Script → トリガー → onEditTrigger / スプレッドシートから / 編集時
 *
 * Script Properties:
 *   KINTONE_DOMAIN, KINTONE_USERNAME, KINTONE_PASSWORD
 *   GEMINI_API_KEY, SLACK_BOT_TOKEN, SLACK_SHIFT_CHANNEL, SHIFT_CALENDAR_ID
 */

// ==========================================================================
// Constants
// ==========================================================================

var SS_ID = '1JlyWngnuha1IHQLMGs5bzTnjct8s7eY4Z-bW0YHIlmU';
var KINTONE_WISH_APP = 211;
var KINTONE_CONFIRMED_APP = 212;
var KINTONE_STAFF_APP = 213;

var SN_STAFF = 'スタッフマスタ';
var SN_STORE = '店舗マスタ';
var SN_RULES = '共通ルールマスタ';
var SN_WISH = '希望収集データ';
var SN_OUTPUT = 'シフト出力';

var DOW_JP = ['日', '月', '火', '水', '木', '金', '土'];

// Layout constants
var OUTPUT_HROWS = 3;
var OUTPUT_DSTART = 4; // 1-indexed first data row
var OUTPUT_DAYS = 31;
var NON_RETAIL_START_COL = 22; // 非営業店舗セクション開始列 (1-indexed, V列)

// Year/month selector positions (1-indexed)
var YEAR_ROW = 1;   // A1
var YEAR_COL = 1;   // A1
var MONTH_ROW = 2;  // A2
var MONTH_COL = 1;  // A2

// Retail store column map (1-indexed) - FIXED
// Each shift type: {n: name cell col, s: 出勤 time col, e: 退勤 time col}
var RETAIL_COLS = {
  '藤沢':       { '早番': {n:4,s:4,e:5},   '遅番': {n:6,s:6,e:7},   '※予備': {n:8,s:8,e:9} },
  '伊勢佐木町': { '早番': {n:10,s:10,e:11}, '遅番': {n:12,s:12,e:13}, '※予備': {n:14,s:14,e:15} },
  '新宿':       { '早番': {n:16,s:16,e:17}, '遅番': {n:18,s:18,e:19}, '※予備': {n:20,s:20,e:21} },
};
var SHIFT_TYPES = ['早番', '遅番', '※予備'];

// Non-retail store display order (fixed order)
var NON_RETAIL_STORE_ORDER = ['工場', 'EC', '本部オフィス'];

// ==========================================================================
// Menu
// ==========================================================================

// メニューは運用に必要な3項目のみ (2026-07-06 簡素化)
// メニューから外した関数はコードに残置 (アーカイブ):
//   loadMonthData(年月セレクター変更のonEditで自動実行・admin=reloadでも代替) /
//   fetchWishData(希望グリッド再描画=loadMonthDataと重複) / debugProperties(doPost probe/admin で代替) /
//   renderPersonalShift_(セレクターonEdit + admin=personal で自動/代替) /
//   setupTrigger(onEditは主要関数のensureTrigger_で自己修復・全復旧は admin=triggers)
function onOpen() {
  SpreadsheetApp.getUi().createMenu('シフト勤怠管理')
    .addItem('① スタッフマスタ同期', 'syncStaffMaster')
    .addItem('② AIシフト生成', 'generateShift')
    .addItem('③ 確定シフト反映', 'syncConfirmedShift')
    .addToUi();
}

function debugProperties() {
  var props = PropertiesService.getScriptProperties().getProperties();
  var keys = ['GEMINI_API_KEY', 'SLACK_BOT_TOKEN', 'SLACK_SHIFT_CHANNEL', 'SHIFT_CALENDAR_ID', 'WEBHOOK_TOKEN', 'ATTEND_NOTIFY_MIN', 'ATTEND_RENOTIFY_MIN'];
  var lines = [];
  keys.forEach(function(k) {
    var v = props[k];
    var isSecret = (k.indexOf('PASSWORD') >= 0 || k.indexOf('TOKEN') >= 0 || k.indexOf('API_KEY') >= 0);
    if (isSecret) {
      lines.push(k + ': ' + (v ? '[SET, len=' + v.length + ']' : '[EMPTY]'));
    } else {
      lines.push(k + ': ' + (v || '[EMPTY]'));
    }
  });

  // v2: 正本タブとトリガーの状態
  var checks = [];
  try {
    checks.push(SN_SHIFT_DATA + ': ' + (sheet_(SN_SHIFT_DATA) ? (sheet_(SN_SHIFT_DATA).getLastRow() - 1) + '件' : '[未作成]'));
    checks.push(SN_WISH_DATA + ': ' + (sheet_(SN_WISH_DATA) ? (sheet_(SN_WISH_DATA).getLastRow() - 1) + '件' : '[未作成]'));
    checks.push(SN_KINTAI + ': ' + (sheet_(SN_KINTAI) ? (sheet_(SN_KINTAI).getLastRow() - 1) + '件' : '[未作成]'));
    checks.push(SN_PERSONAL + ': ' + (sheet_(SN_PERSONAL) ? 'あり' : '[未作成]'));
    var triggerNames = ScriptApp.getProjectTriggers().map(function(t) { return t.getHandlerFunction(); });
    checks.push('トリガー: ' + (triggerNames.join(', ') || 'なし'));
  } catch (e) {
    checks.push('チェックエラー: ' + e.message);
  }

  SpreadsheetApp.getUi().alert(
    '--- Script Properties ---\n' + lines.join('\n') +
    '\n\n--- v2 正本タブ / トリガー ---\n' + checks.join('\n'));
}

// ==========================================================================
// シフト希望収集 自動通知 (dailyShiftReminder - 毎日トリガーで実行)
// ==========================================================================

/**
 * 毎日実行: 日付に応じてシフト希望収集の開始通知/リマインドを送信。
 *
 * スケジュール:
 *   1日: 当月後半(16日〜末日)の希望収集開始通知, 締切10日
 *   15日: 翌月前半(1日〜15日)の希望収集開始通知, 締切25日
 *   5日,8日,10日: 10日締切のリマインド (5日前,2日前,当日)
 *   20日,23日,25日: 25日締切のリマインド (5日前,2日前,当日)
 *   11日以降(偶数日): 10日締切超過リマインド(1日おき)
 *   26日以降(偶数日): 25日締切超過リマインド(1日おき)
 */
function dailyShiftReminder() {
  var today = new Date();
  var day = today.getDate();
  var month = today.getMonth() + 1; // 1-indexed
  var year = today.getFullYear();

  // Determine which collection period to check
  var periodInfo = null;

  // --- 10日締切: 当月後半 ---
  if (day === 1) {
    // 収集開始通知
    var lastDay = new Date(year, month, 0).getDate();
    periodInfo = { type: 'start', periodStart: year + '-' + pad2(month) + '-16', periodEnd: year + '-' + pad2(month) + '-' + lastDay, deadline: month + '/10' };
  } else if (day === 5 || day === 8 || day === 10) {
    var lastDay = new Date(year, month, 0).getDate();
    var daysLeft = 10 - day;
    periodInfo = { type: 'remind', periodStart: year + '-' + pad2(month) + '-16', periodEnd: year + '-' + pad2(month) + '-' + lastDay, deadline: month + '/10', daysLeft: daysLeft };
  } else if (day > 10 && day <= 14 && day % 2 === 0) {
    var lastDay = new Date(year, month, 0).getDate();
    periodInfo = { type: 'overdue', periodStart: year + '-' + pad2(month) + '-16', periodEnd: year + '-' + pad2(month) + '-' + lastDay, deadline: month + '/10' };
  }

  // --- 25日締切: 翌月前半 ---
  if (day === 15) {
    var nextMonth = month === 12 ? 1 : month + 1;
    var nextYear = month === 12 ? year + 1 : year;
    periodInfo = { type: 'start', periodStart: nextYear + '-' + pad2(nextMonth) + '-01', periodEnd: nextYear + '-' + pad2(nextMonth) + '-15', deadline: month + '/25' };
  } else if (day === 20 || day === 23 || day === 25) {
    var nextMonth = month === 12 ? 1 : month + 1;
    var nextYear = month === 12 ? year + 1 : year;
    var daysLeft = 25 - day;
    periodInfo = { type: 'remind', periodStart: nextYear + '-' + pad2(nextMonth) + '-01', periodEnd: nextYear + '-' + pad2(nextMonth) + '-15', deadline: month + '/25', daysLeft: daysLeft };
  } else if (day > 25 && day % 2 === 0) {
    var nextMonth = month === 12 ? 1 : month + 1;
    var nextYear = month === 12 ? year + 1 : year;
    periodInfo = { type: 'overdue', periodStart: nextYear + '-' + pad2(nextMonth) + '-01', periodEnd: nextYear + '-' + pad2(nextMonth) + '-15', deadline: month + '/25' };
  }

  if (!periodInfo) return; // 通知不要の日

  // Get all staff
  var staff = readStaffMaster_();

  if (periodInfo.type === 'start') {
    // v2: 希望収集データシートを対象月に切り替え (希望データタブから再描画・雛形生成は不要)
    var pYear = periodInfo.periodStart.split('-')[0];
    var pMonth = String(parseInt(periodInfo.periodStart.split('-')[1], 10));
    var wishSheet = sheet_(SN_WISH);
    if (wishSheet) {
      wishSheet.getRange(1, 1).setValue(parseInt(pYear));
      wishSheet.getRange(2, 1).setValue(parseInt(pMonth));
      try { loadWishSheetData_(pYear, pMonth); } catch (e2) { Logger.log('wish grid prepare: ' + e2.message); }
    }

    // チャンネル通知
    var text = '*シフト希望入力を開始してください*\n'
      + '対象期間: ' + periodInfo.periodStart + ' 〜 ' + periodInfo.periodEnd + '\n'
      + '締切: *' + periodInfo.deadline + '*\n'
      + '希望シフトスタッフ → DMのボタンから出勤日時を入力\n'
      + '固定シフトスタッフ → DMのボタンから希望休を入力';
    slackPost_(text);

    // 個別DM送信
    var dmCount = sendShiftCollectionDMs_(staff, periodInfo.periodStart, periodInfo.periodEnd, periodInfo.deadline);
    Logger.log('Shift collection start: ' + dmCount + ' DMs sent');
    return;
  }

  // リマインド / 期限超過: 未入力者を検出
  var unsubmitted = findUnsubmittedStaff_(staff, periodInfo.periodStart, periodInfo.periodEnd);

  if (unsubmitted.length === 0) {
    Logger.log('All staff submitted for ' + periodInfo.periodStart + ' ~ ' + periodInfo.periodEnd);
    return;
  }

  // Build mention list
  var mentions = unsubmitted.map(function(s) {
    return s.slackId ? '<@' + s.slackId + '>' : s.name;
  }).join(', ');

  var text;
  if (periodInfo.type === 'remind') {
    if (periodInfo.daysLeft > 0) {
      text = '*シフト希望 未入力のお知らせ*\n'
        + '対象期間: ' + periodInfo.periodStart + ' 〜 ' + periodInfo.periodEnd + '\n'
        + '締切: *' + periodInfo.deadline + '* (あと' + periodInfo.daysLeft + '日)\n'
        + '未入力: ' + mentions;
    } else {
      text = '*本日シフト希望の締切です！*\n'
        + '対象期間: ' + periodInfo.periodStart + ' 〜 ' + periodInfo.periodEnd + '\n'
        + '未入力: ' + mentions + '\n'
        + '至急入力をお願いします。';
    }
  } else if (periodInfo.type === 'overdue') {
    text = '*⚠ シフト希望 提出期限超過*\n'
      + '対象期間: ' + periodInfo.periodStart + ' 〜 ' + periodInfo.periodEnd + '\n'
      + '締切 ' + periodInfo.deadline + ' を過ぎています。\n'
      + '未入力: ' + mentions + '\n'
      + '至急入力してください！';
  }

  if (text) {
    slackPost_(text);
    Logger.log('Shift reminder sent (' + periodInfo.type + '): ' + unsubmitted.length + ' unsubmitted');
  }
}

function pad2(n) { return ('0' + n).slice(-2); }

/**
 * 指定期間の未入力スタッフを検出
 */
function findUnsubmittedStaff_(staff, periodStart, periodEnd) {
  // Count expected days in period
  var startDate = new Date(periodStart + 'T00:00:00');
  var endDate = new Date(periodEnd + 'T00:00:00');
  var expectedDays = Math.round((endDate - startDate) / (1000 * 60 * 60 * 24)) + 1;

  // 希望データ(正本タブ)から期間内の入力を取得 (kintone 211 の置き換え)
  var records = readWishDataRange_(periodStart, periodEnd);

  // Count records per staff
  var submittedCount = {};
  records.forEach(function(r) {
    var sid = r.staff_id;
    if (!submittedCount[sid]) submittedCount[sid] = 0;
    submittedCount[sid]++;
  });

  // Find staff with 0 records (no submission at all)
  var unsubmitted = [];
  staff.forEach(function(s) {
    var sid = s['staff_id'];
    var count = submittedCount[sid] || 0;
    if (count === 0) {
      unsubmitted.push({
        staffId: sid,
        name: s['氏名'],
        slackId: s['Slack ID'] || '',
      });
    }
  });

  return unsubmitted;
}

/**
 * 希望収集リマインドのトリガーを設定（初回1回だけ実行）
 */
function setupDailyReminder() {
  var triggers = ScriptApp.getProjectTriggers();
  for (var i = 0; i < triggers.length; i++) {
    if (triggers[i].getHandlerFunction() === 'dailyShiftReminder') {
      SpreadsheetApp.getUi().alert('dailyShiftReminder は既に設定済みです。');
      return;
    }
  }
  ScriptApp.newTrigger('dailyShiftReminder')
    .timeBased()
    .atHour(9)
    .everyDays(1)
    .create();
  SpreadsheetApp.getUi().alert('希望収集リマインドを設定しました。\n毎朝9時に自動実行されます。');
}

/**
 * installable onEdit trigger を設定する（初回1回だけ実行）。
 * 設定後は年月セレクターの変更で自動的にデータが読み込まれる。
 */
function setupTrigger() {
  var created = ensureTrigger_();
  var attCreated = ensureAttendanceTrigger_();
  var msgs = [];
  msgs.push(created ? 'onEditTrigger: 新規作成' : 'onEditTrigger: 設定済み');
  msgs.push(attCreated ? 'attendanceCron (出勤確認・10分毎): 新規作成' : 'attendanceCron: 設定済み');
  SpreadsheetApp.getUi().alert('トリガー設定\n\n' + msgs.join('\n') +
    '\n\n年月セレクターを変更すると、シフトデータ/希望データ(スプシ内の正本タブ)から自動で読み込まれます。');
}

/**
 * onEditTrigger が未設定なら自動で作成する。全メニュー関数から呼ばれる。
 * @return {boolean} true=新規作成, false=既存
 */
function ensureTrigger_() {
  var triggers = ScriptApp.getProjectTriggers();
  for (var i = 0; i < triggers.length; i++) {
    if (triggers[i].getHandlerFunction() === 'onEditTrigger') {
      return false;
    }
  }
  ScriptApp.newTrigger('onEditTrigger')
    .forSpreadsheet(SS_ID)
    .onEdit()
    .create();
  Logger.log('onEditTrigger auto-created');
  return true;
}

// ==========================================================================
// Installable onEdit trigger (年月セレクター連動)
// ==========================================================================

/**
 * installable onEdit trigger.
 * Apps Script → トリガー → 関数: onEditTrigger / イベント: スプレッドシートから・編集時
 */
function onEditTrigger(e) {
  if (!e || !e.range) return;
  var sheet = e.range.getSheet();
  // gid → 論理名 (リネーム耐性)。未知のシートは実名のまま。
  var gid = sheet.getSheetId();
  var name = sheet.getName();
  for (var gname in SN_GID_MAP_) {
    if (SN_GID_MAP_[gname] === gid) { name = gname; break; }
  }
  var row = e.range.getRow();
  var col = e.range.getColumn();

  // Year/month selector: シフト出力 A1:B1=year(merged), A2:B2=month(merged)
  if (name === SN_OUTPUT && (col === 1 || col === 2) && (row === YEAR_ROW || row === MONTH_ROW)) {
    var year = String(sheet.getRange(YEAR_ROW, YEAR_COL).getValue()).trim();
    var month = String(sheet.getRange(MONTH_ROW, MONTH_COL).getValue()).trim();
    if (year && month) {
      loadShiftOutputData_(year, month);
    }
    return;
  }
  if (name === SN_WISH && col === 1 && (row === 1 || row === 2)) {
    var year = String(sheet.getRange(1, 1).getValue()).trim();
    var month = String(sheet.getRange(2, 1).getValue()).trim();
    if (year && month) {
      loadWishSheetData_(year, month);
    }
    return;
  }

  // 個人シフト: セレクター (A1=年 / A2=月 / C2=名前) 連動
  if (name === SN_PERSONAL && col <= 3 && row <= 2) {
    try { renderPersonalShift_(); } catch (pe) { Logger.log('personal render: ' + pe.message); }
    return;
  }

  // 希望収集データ: ×連動 (出勤に×→退勤も×、退勤に×→出勤も×)
  if (name === SN_WISH && row >= 4) {
    var val = String(e.range.getValue()).trim();
    // Read Row 3 sub-headers to find 出勤/退勤 pairs
    var subRow = sheet.getRange(3, 1, 1, sheet.getLastColumn()).getValues()[0];
    var sub = (col - 1 < subRow.length) ? String(subRow[col - 1]).trim() : '';

    if (sub === '出勤' || sub === '退勤') {
      var pairCol = (sub === '出勤') ? col + 1 : col - 1;
      var pairSub = (pairCol - 1 >= 0 && pairCol - 1 < subRow.length) ? String(subRow[pairCol - 1]).trim() : '';

      if ((sub === '出勤' && pairSub === '退勤') || (sub === '退勤' && pairSub === '出勤')) {
        if (val === '×' || val === 'x' || val === '✕') {
          sheet.getRange(row, pairCol).setValue('×');
          // Apply gray background to both cells
          sheet.getRange(row, Math.min(col, pairCol), 1, 2).setBackground('#9E9E9E');
        } else if (val === '') {
          // Clear pair too
          sheet.getRange(row, pairCol).setValue('');
          sheet.getRange(row, Math.min(col, pairCol), 1, 2).setBackground(null);
        }
      }
    }

    // v2: 手編集を希望データ(正本タブ)へ同期
    if (sub === '出勤' || sub === '退勤' || sub === '希望休') {
      try { syncWishCellToData_(sheet, row, col, sub); } catch (we) { Logger.log('wish sync: ' + we.message); }
    }
    return;
  }

  // 店舗マスタ: 勤務開始で×→勤務終了も× (F=col6, G=col7)
  if (name === SN_STORE && row >= 2 && (col === 6 || col === 7)) {
    var val = String(e.range.getValue()).trim();
    var pairCol = (col === 6) ? 7 : 6;
    if (val === '×' || val === 'x') {
      sheet.getRange(row, pairCol).setValue('×');
    } else if (val === '') {
      sheet.getRange(row, pairCol).setValue('');
    }
  }
}

/**
 * ⑤ 月データ読込 (メニューから手動実行)
 */
function loadMonthData() {
  ensureTrigger_();
  var ui = SpreadsheetApp.getUi();
  try {
    var outputSheet = sheet_(SN_OUTPUT);

    var year = String(outputSheet.getRange(YEAR_ROW, YEAR_COL).getValue()).trim();
    var month = String(outputSheet.getRange(MONTH_ROW, MONTH_COL).getValue()).trim();
    if (!year || !month) { ui.alert('年月セレクター(シフト出力 A1=年/A2=月)を設定してください'); return; }

    var msg1 = loadShiftOutputData_(year, month);
    var msg2 = loadWishSheetData_(year, month);

    ui.alert(
      '月データ読込完了 (' + year + '年' + month + '月)\n\n' +
      'シフト出力: ' + msg1 + '\n' +
      '希望収集データ: ' + msg2
    );
  } catch (e) {
    slackError_('loadMonthData', e.message);
    ui.alert('エラー: ' + e.message);
  }
}

// ==========================================================================
// Data loading from kintone
// ==========================================================================

/**
 * kintone 212 (確定シフト) → シフト出力シートに表示
 */
function loadShiftOutputData_(year, month) {
  var outputSheet = sheet_(SN_OUTPUT);
  if (!outputSheet) return 'シート未検出';

  var m = ('0' + month).slice(-2);
  var firstDate = year + '-' + m + '-01';
  var lastDay = new Date(parseInt(year), parseInt(month), 0).getDate();
  var lastDate = year + '-' + m + '-' + ('0' + lastDay).slice(-2);
  var totalRows = lastDay * 2;

  // Read dynamic non-retail layout from headers
  var nrLayout = readNonRetailLayout_(outputSheet);
  var OUTPUT_TOTAL_COLS = nrLayout.totalCols;

  // Clear data values only (preserve formatting)
  outputSheet.getRange(OUTPUT_DSTART, 1, OUTPUT_DAYS * 2, OUTPUT_TOTAL_COLS).clearContent();
  // 迷い背景の掃除: グリッド幅の外側(過去レイアウトの残骸)も含めて背景をリセット
  outputSheet.getRange(OUTPUT_DSTART, 1, OUTPUT_DAYS * 2, outputSheet.getMaxColumns()).setBackground(null);

  // Build 2D grids: values, backgrounds, font colors
  var grid = [];       // 2D array of cell values
  var bgGrid = [];     // 2D array of background colors
  for (var r = 0; r < totalRows; r++) {
    var valRow = [];
    var bgRow = [];
    for (var c = 0; c < OUTPUT_TOTAL_COLS; c++) {
      valRow.push('');
      bgRow.push('#ffffff');
    }
    grid.push(valRow);
    bgGrid.push(bgRow);
  }

  // Helper: set cell in grid (0-indexed row within data area, 1-indexed col)
  function setCell(gridRowIdx, col1, value) {
    if (gridRowIdx >= 0 && gridRowIdx < totalRows && col1 >= 1 && col1 <= OUTPUT_TOTAL_COLS) {
      grid[gridRowIdx][col1 - 1] = value;
    }
  }
  function setBg(gridRowIdx, col1, color) {
    if (gridRowIdx >= 0 && gridRowIdx < totalRows && col1 >= 1 && col1 <= OUTPUT_TOTAL_COLS) {
      bgGrid[gridRowIdx][col1 - 1] = color;
    }
  }

  // Fill dates and weekend colors
  for (var day = 1; day <= lastDay; day++) {
    var d = new Date(parseInt(year), parseInt(month) - 1, day);
    var nameIdx = (day - 1) * 2;      // grid row index for name row
    var timeIdx = nameIdx + 1;         // grid row index for time row
    var dateLabel = (d.getMonth() + 1) + '/' + d.getDate();
    var dow = DOW_JP[d.getDay()];

    setCell(nameIdx, 3, dateLabel);
    setCell(timeIdx, 3, dow);

    // Weekend background for entire row
    var weekendColor = (d.getDay() === 0) ? '#F4C7C3' : (d.getDay() === 6) ? '#B4D7F0' : null;
    if (weekendColor) {
      for (var c = 0; c < OUTPUT_TOTAL_COLS; c++) {
        bgGrid[nameIdx][c] = weekendColor;
        bgGrid[timeIdx][c] = weekendColor;
      }
    }
  }

  // シフトデータ(正本タブ)から期間内レコードを取得 (kintone 212 の置き換え・件数上限なし)
  var records = readShiftDataRange_(firstDate, lastDate);
  if (!records.length) {
    // Write grid even if no records (dates + weekend colors)
    outputSheet.getRange(OUTPUT_DSTART, 1, totalRows, OUTPUT_TOTAL_COLS).setValues(grid);
    // 背景はメモ列を除いて塗る (メモ列は結合セルがあり色が隣へ広がって見えるため常に白)
    var bgW0 = OUTPUT_TOTAL_COLS - 1;
    outputSheet.getRange(OUTPUT_DSTART, 1, totalRows, bgW0)
      .setBackgrounds(bgGrid.map(function(r) { return r.slice(0, bgW0); }));
    applyOutputValidations_(outputSheet, lastDay, nrLayout);
    return lastDay + '日分の日付を表示 (確定データ0件)';
  }

  // Build day lookup
  var dayData = {};
  records.forEach(function(r) {
    var day = parseInt(r.date.split('-')[2], 10);
    if (!dayData[day]) dayData[day] = [];
    dayData[day].push({
      staff_name: r.staff_name,
      store: r.store,
      start_time: r.start_time,
      end_time: r.end_time,
      shift_type: '',
      shift_status: r.shift_status,
    });
  });

  // Process each day
  var written = 0;
  var KOKYU_COLOR = '#9E9E9E';

  for (var day = 1; day <= lastDay; day++) {
    var entries = dayData[day] || [];
    var nameIdx = (day - 1) * 2;
    var timeIdx = nameIdx + 1;
    var usedSlots = {};

    // kintoneにデータがある日は確定チェックをONにする
    if (entries.length > 0) {
      setCell(nameIdx, 1, true);   // 確定=ON
    }
    setCell(nameIdx, 2, false);    // 変更=OFF (常にリセット)

    // Collect working staff
    var workingStaff = {};
    entries.forEach(function(entry) {
      if (entry.shift_status === '出勤') workingStaff[entry.staff_name] = true;
    });

    // 公休 for non-retail staff (dynamic)
    function fillKokyu(personCols, personNames) {
      for (var pi = 0; pi < personNames.length; pi++) {
        if (!personNames[pi] || workingStaff[personNames[pi]]) continue;
        if (pi < personCols.length) {
          var pc = personCols[pi];
          setCell(nameIdx, pc.s, '公休');
          setCell(timeIdx, pc.s, '×');
          setCell(timeIdx, pc.e, '×');
          for (var col = pc.s; col <= pc.e; col++) {
            setBg(nameIdx, col, KOKYU_COLOR);
            setBg(timeIdx, col, KOKYU_COLOR);
          }
        }
      }
    }
    for (var storeName in nrLayout.stores) {
      var storeInfo = nrLayout.stores[storeName];
      fillKokyu(storeInfo.cols, storeInfo.names);
    }

    // Process 出勤 entries
    entries.forEach(function(entry) {
      if (entry.shift_status !== '出勤') return;
      var store = entry.store;
      var stype = entry.shift_type;

      // Infer shift_type
      if (RETAIL_COLS[store] && !RETAIL_COLS[store][stype]) {
        if (entry.start_time) {
          var startHour = parseInt(entry.start_time.split(':')[0], 10);
          stype = (startHour < 13) ? '早番' : '遅番';
        } else {
          stype = '早番';
        }
        var slotKey = store + '_' + stype + '_' + day;
        if (usedSlots[slotKey]) { stype = (stype === '早番') ? '遅番' : '※予備'; slotKey = store + '_' + stype + '_' + day; }
        if (usedSlots[slotKey]) { stype = '※予備'; }
      }

      // Retail stores
      if (RETAIL_COLS[store] && RETAIL_COLS[store][stype]) {
        usedSlots[store + '_' + stype + '_' + day] = true;
        var cols = RETAIL_COLS[store][stype];
        setCell(nameIdx, cols.n, entry.staff_name);
        if (entry.start_time) setCell(timeIdx, cols.s, entry.start_time);
        if (entry.end_time) setCell(timeIdx, cols.e, entry.end_time);
        written++;
        return;
      }

      // Non-retail (dynamic)
      var storeLayout = nrLayout.stores[store];
      if (!storeLayout) return;
      var personCols = storeLayout.cols;
      var personNames = storeLayout.names;

      var idx = personNames.indexOf(entry.staff_name);
      if (idx < 0) {
        for (var i = 0; i < personNames.length; i++) {
          if (!personNames[i]) { idx = i; personNames[i] = entry.staff_name; break; }
        }
      }
      if (idx >= 0 && idx < personCols.length) {
        var pc = personCols[idx];
        setCell(nameIdx, pc.s, '出勤');
        if (entry.start_time) setCell(timeIdx, pc.s, entry.start_time);
        if (entry.end_time) setCell(timeIdx, pc.e, entry.end_time);
        written++;
      }
    });
  }

  // 兼務スタッフの店舗自動表示: 非営業店舗列に所属するスタッフが
  // 営業店舗にも配置されている場合、非営業列に店舗名+時間を表示
  for (var storeName in nrLayout.stores) {
    var storeInfo = nrLayout.stores[storeName];
    for (var pi = 0; pi < storeInfo.names.length; pi++) {
      var pName = storeInfo.names[pi];
      if (!pName) continue;
      var pc = storeInfo.cols[pi];
      for (var day = 1; day <= lastDay; day++) {
        var nameIdx = (day - 1) * 2;
        var timeIdx = nameIdx + 1;
        var currentVal = grid[nameIdx][pc.s - 1];
        if (currentVal === '出勤') continue;
        if (currentVal === '公休') {
          // 公休の場合、時間行に×を入れる
          grid[timeIdx][pc.s - 1] = '×';
          grid[timeIdx][pc.e - 1] = '×';
          continue;
        }
        var entries = dayData[day] || [];
        for (var ei = 0; ei < entries.length; ei++) {
          var e = entries[ei];
          if (e.staff_name === pName && e.shift_status === '出勤' && RETAIL_COLS[e.store]) {
            grid[nameIdx][pc.s - 1] = e.store;
            if (e.start_time) grid[timeIdx][pc.s - 1] = e.start_time;
            if (e.end_time) grid[timeIdx][pc.e - 1] = e.end_time;
            break;
          }
        }
      }
    }
  }

  // Apply values and backgrounds only (preserve manual formatting)
  outputSheet.getRange(OUTPUT_DSTART, 1, totalRows, OUTPUT_TOTAL_COLS).setValues(grid);
  var bgW = OUTPUT_TOTAL_COLS - 1;
  outputSheet.getRange(OUTPUT_DSTART, 1, totalRows, bgW)
    .setBackgrounds(bgGrid.map(function(r) { return r.slice(0, bgW); }));

  // Re-merge non-retail name-row cells (setValues breaks merges)
  for (var storeName in nrLayout.stores) {
    var storeInfo = nrLayout.stores[storeName];
    for (var pi = 0; pi < storeInfo.cols.length; pi++) {
      var pc = storeInfo.cols[pi];
      for (var day = 1; day <= lastDay; day++) {
        var nameRowNum = OUTPUT_DSTART + (day - 1) * 2;
        outputSheet.getRange(nameRowNum, pc.s, 1, 2).merge();
      }
    }
  }

  applyOutputValidations_(outputSheet, lastDay, nrLayout);

  return records.length + '件読込 (' + written + '配置)';
}

/**
 * シフト出力(シフト作成)シートの検証ルールを再適用する。
 * 確定データ0件でも必ず呼ばれる (プルダウン/チェックボックスの復元)。
 */
function applyOutputValidations_(outputSheet, lastDay, nrLayout) {
  // Checkbox validation on columns A-B for name rows
  var checkRule = SpreadsheetApp.newDataValidation().requireCheckbox().build();
  for (var day = 1; day <= lastDay; day++) {
    outputSheet.getRange(OUTPUT_DSTART + (day - 1) * 2, 1, 1, 2).setDataValidation(checkRule);
  }

  // Re-apply dropdowns for non-retail person columns (dynamic)
  var attendRule = SpreadsheetApp.newDataValidation().requireValueInList(['', '出勤', '公休'], true).setAllowInvalid(true).build();
  // 兼務スタッフ用: 出勤/店舗/公休
  var retailStoreNames = Object.keys(RETAIL_COLS);
  var dualRule = SpreadsheetApp.newDataValidation().requireValueInList(['', '出勤', '公休'].concat(retailStoreNames), true).setAllowInvalid(true).build();
  var timeOptions = ['','×','9:00','9:30','10:00','10:30','11:00','11:30','12:00','12:30','13:00','13:30','14:00','14:30','15:00','15:30','16:00','16:30','17:00','17:30','18:00','18:30','19:00','19:30','20:00','20:30','21:00','21:30','22:00','22:30','23:00','23:30','0:00','0:30'];
  var timeRule = SpreadsheetApp.newDataValidation().requireValueInList(timeOptions, true).setAllowInvalid(true).build();

  // Read staff master to identify dual-store staff
  var staffMaster = readStaffMaster_();
  var dualStoreStaff = {};
  staffMaster.forEach(function(s) {
    var stores = (s['対応店舗'] || '').split(', ');
    var hasRetail = stores.some(function(st) { return !!RETAIL_COLS[st]; });
    var hasNonRetail = stores.some(function(st) { return !RETAIL_COLS[st] && st; });
    if (hasRetail && hasNonRetail) dualStoreStaff[s['氏名']] = true;
  });

  // Retail time dropdowns
  for (var day = 1; day <= lastDay; day++) {
    var timeRowNum = OUTPUT_DSTART + (day - 1) * 2 + 1;
    for (var store in RETAIL_COLS) {
      var shifts = RETAIL_COLS[store];
      for (var st = 0; st < SHIFT_TYPES.length; st++) {
        var cols = shifts[SHIFT_TYPES[st]];
        outputSheet.getRange(timeRowNum, cols.s).setDataValidation(timeRule);
        outputSheet.getRange(timeRowNum, cols.e).setDataValidation(timeRule);
      }
    }
  }

  // Non-retail dropdowns
  var allPersonCols = nrLayout.allPersonCols;
  for (var pi = 0; pi < allPersonCols.length; pi++) {
    var pc = allPersonCols[pi];
    // Check if this person is a dual-store staff
    var personName = '';
    for (var sn in nrLayout.stores) {
      var si = nrLayout.stores[sn];
      for (var ci = 0; ci < si.cols.length; ci++) {
        if (si.cols[ci].s === pc.s) { personName = si.names[ci]; break; }
      }
      if (personName) break;
    }
    var nameRule = dualStoreStaff[personName] ? dualRule : attendRule;
    for (var day = 1; day <= lastDay; day++) {
      var nameRowNum = OUTPUT_DSTART + (day - 1) * 2;
      var timeRowNum = nameRowNum + 1;
      outputSheet.getRange(nameRowNum, pc.s).setDataValidation(nameRule);
      outputSheet.getRange(timeRowNum, pc.s).setDataValidation(timeRule);
      outputSheet.getRange(timeRowNum, pc.e).setDataValidation(timeRule);
    }
  }

}

/**
 * kintone 211 (希望収集) → 希望収集データシートに表示
 */
function loadWishSheetData_(year, month) {
  var wishSheet = sheet_(SN_WISH);
  if (!wishSheet) return 'シート未検出';

  var m = ('0' + month).slice(-2);
  var firstDate = year + '-' + m + '-01';
  var lastDay = new Date(parseInt(year), parseInt(month), 0).getDate();
  var lastDate = year + '-' + m + '-' + ('0' + lastDay).slice(-2);

  // New layout: Row 2 = staff names, Row 3 = sub-headers, Row 4+ = data
  // Left side (C onward, 2 cols each): 希望シフトスタッフ (出勤/退勤)
  // Right side: 固定シフトスタッフ (希望休, 1 col each)
  var lastCol = wishSheet.getLastColumn();
  var nameRow = wishSheet.getRange(2, 1, 1, lastCol).getValues()[0];
  var subRow = wishSheet.getRange(3, 1, 1, lastCol).getValues()[0];

  // Parse wish staff (出勤/退勤 pairs from col C onward)
  var wishStaffNames = [];
  var wishStaffCols = [];  // 0-indexed start col for each wish staff
  for (var c = 2; c < nameRow.length; c++) {
    var name = String(nameRow[c]).trim();
    var sub = String(subRow[c]).trim();
    if (name && sub === '出勤') {
      wishStaffNames.push(name);
      wishStaffCols.push(c);
    }
  }

  // Parse fixed staff (希望休, 1 col each)
  var fixedStaffNames = [];
  var fixedStaffCols = [];
  for (var c = 2; c < nameRow.length; c++) {
    var name = String(nameRow[c]).trim();
    var sub = String(subRow[c]).trim();
    if (name && sub === '希望休') {
      fixedStaffNames.push(name);
      fixedStaffCols.push(c);
    }
  }

  // Staff name → staff_id lookup
  var staff = readStaffMaster_();
  var nameToSid = {};
  staff.forEach(function(s) { nameToSid[s['氏名']] = s['staff_id']; });

  // Determine total columns needed
  var totalCols = Math.max(lastCol, 12);
  var KOKYU_COLOR = '#9E9E9E';

  // Clear data area
  wishSheet.getRange(4, 1, 31, totalCols).clearContent();

  // Build 2D grids for values and backgrounds
  var grid = [];
  var bgGrid = [];
  for (var r = 0; r < lastDay; r++) {
    var valRow = [];
    var bgRow = [];
    for (var c = 0; c < totalCols; c++) { valRow.push(''); bgRow.push('#ffffff'); }
    grid.push(valRow);
    bgGrid.push(bgRow);
  }

  // Fill dates + weekend colors
  for (var day = 1; day <= lastDay; day++) {
    var d = new Date(parseInt(year), parseInt(month) - 1, day);
    var idx = day - 1;
    grid[idx][0] = (d.getMonth() + 1) + '/' + d.getDate();
    grid[idx][1] = DOW_JP[d.getDay()];
    var weekendColor = (d.getDay() === 0) ? '#F4C7C3' : (d.getDay() === 6) ? '#B4D7F0' : null;
    if (weekendColor) {
      for (var c = 0; c < totalCols; c++) { bgGrid[idx][c] = weekendColor; }
    }
  }

  // 希望データ(正本タブ)から期間内の入力を取得 (kintone 211 の置き換え)
  var records = readWishDataRange_(firstDate, lastDate);
  if (!records.length) {
    wishSheet.getRange(4, 1, lastDay, totalCols).setValues(grid);
    wishSheet.getRange(4, 1, lastDay, totalCols).setBackgrounds(bgGrid);
    return lastDay + '日分の日付を表示 (希望データ0件)';
  }

  // Build lookup
  var wishes = {};
  records.forEach(function(r) {
    if (!r.staff_id || !r.date) return;
    var day = parseInt(r.date.split('-')[2], 10);
    if (!wishes[r.staff_id]) wishes[r.staff_id] = {};
    wishes[r.staff_id][day] = {
      shift_type: r.wish_type || '出勤',
      start_time: r.start_time,
      end_time: r.end_time,
    };
  });

  // Fill wish staff data into grid
  var written = 0;
  for (var si = 0; si < wishStaffNames.length; si++) {
    var sname = wishStaffNames[si];
    var sid = nameToSid[sname];
    if (!sid) continue;
    var sw = wishes[sid] || {};
    var startColIdx = wishStaffCols[si];  // 0-indexed
    var endColIdx = startColIdx + 1;

    for (var day = 1; day <= lastDay; day++) {
      var dw = sw[day];
      if (!dw) continue;
      var idx = day - 1;

      if (dw.shift_type === '休み' || dw.shift_type === '希望休' || dw.shift_type === '休み希望') {
        grid[idx][startColIdx] = '×';
        grid[idx][endColIdx] = '×';
        bgGrid[idx][startColIdx] = KOKYU_COLOR;
        bgGrid[idx][endColIdx] = KOKYU_COLOR;
      } else {
        if (dw.start_time) grid[idx][startColIdx] = dw.start_time;
        if (dw.end_time) grid[idx][endColIdx] = dw.end_time;
      }
      written++;
    }
  }

  // Fill fixed staff data into grid
  for (var si = 0; si < fixedStaffNames.length; si++) {
    var sname = fixedStaffNames[si];
    var sid = nameToSid[sname];
    if (!sid) continue;
    var sw = wishes[sid] || {};
    var colIdx = fixedStaffCols[si];  // 0-indexed

    for (var day = 1; day <= lastDay; day++) {
      var dw = sw[day];
      if (!dw) continue;
      var idx = day - 1;

      if (dw.shift_type === '休み' || dw.shift_type === '希望休' || dw.shift_type === '休み希望') {
        grid[idx][colIdx] = '希望休';
        bgGrid[idx][colIdx] = KOKYU_COLOR;
        written++;
      }
    }
  }

  // Apply values and backgrounds (preserve manual formatting)
  wishSheet.getRange(4, 1, lastDay, totalCols).setValues(grid);
  wishSheet.getRange(4, 1, lastDay, totalCols).setBackgrounds(bgGrid);

  // Re-apply dropdowns for wish staff (出勤/退勤) and fixed staff (希望休)
  var timeOptions = ['×','9:00','9:30','10:00','10:30','11:00','11:30','12:00','12:30','13:00','13:30','14:00','14:30','15:00','15:30','16:00','16:30','17:00','17:30','18:00','18:30','19:00','19:30','20:00','20:30','21:00','21:30','22:00','22:30','23:00','23:30','0:00','0:30'];
  var timeRule = SpreadsheetApp.newDataValidation().requireValueInList(timeOptions, true).setAllowInvalid(true).build();
  for (var si = 0; si < wishStaffCols.length; si++) {
    var startCol = wishStaffCols[si] + 1;
    var endCol = startCol + 1;
    wishSheet.getRange(4, startCol, lastDay, 1).setDataValidation(timeRule);
    wishSheet.getRange(4, endCol, lastDay, 1).setDataValidation(timeRule);
  }

  var fixedOptions = ['', '希望休'];
  var fixedRule = SpreadsheetApp.newDataValidation().requireValueInList(fixedOptions, true).setAllowInvalid(true).build();
  for (var si = 0; si < fixedStaffCols.length; si++) {
    var col = fixedStaffCols[si] + 1;
    wishSheet.getRange(4, col, lastDay, 1).setDataValidation(fixedRule);
  }

  return records.length + '件読込 (' + written + '配置)';
}

// ==========================================================================
// Helpers
// ==========================================================================

var CONFIG_DEFAULTS_ = {
  'KINTONE_DOMAIN': 'ny76p.cybozu.com',
  'KINTONE_USERNAME': '',
  'KINTONE_PASSWORD': '',
  'GEMINI_API_KEY': '',
  'SLACK_BOT_TOKEN': '',
  'SLACK_SHIFT_CHANNEL': 'C0AKBJ1LTV2',
  'SHIFT_CALENDAR_ID': '',
};

function getProp_(key) {
  var val = PropertiesService.getScriptProperties().getProperty(key);
  return val || CONFIG_DEFAULTS_[key] || '';
}

function ss_() { return SpreadsheetApp.openById(SS_ID); }

// シートは gid (不変) で解決し、名前は表示用とフォールバックに使う (ユーザーのリネームに耐える)
var SN_GID_MAP_ = {
  'スタッフマスタ': 1657902443,   // 現名: S
  '店舗マスタ': 375450408,        // 現名: T
  '共通ルールマスタ': 456922734,  // 現名: R
  '希望収集データ': 1764958489,   // 現名: 希望シフト
  'シフト出力': 1533022256,       // 現名: シフト作成
  '個人シフト': 363542595,
  'シフトデータ': 712324603,
  '希望データ': 1404449834,
  '勤怠': 2007718750,
};
function sheetByGid_(gid) {
  var sheets = ss_().getSheets();
  for (var i = 0; i < sheets.length; i++) {
    if (sheets[i].getSheetId() === gid) return sheets[i];
  }
  return null;
}
function sheet_(name) {
  var gid = SN_GID_MAP_[name];
  if (gid) {
    var sh = sheetByGid_(gid);
    if (sh) return sh;
  }
  return ss_().getSheetByName(name);
}

function sheetData_(name) {
  var s = sheet_(name);
  if (!s) return [];
  return s.getDataRange().getValues();
}

function fmtDate_(d) {
  return Utilities.formatDate(d, Session.getScriptTimeZone(), 'yyyy-MM-dd');
}

function generateDates_(startStr, endStr) {
  var dates = [];
  var d = new Date(startStr + 'T00:00:00');
  var end = new Date(endStr + 'T00:00:00');
  while (d <= end) {
    dates.push(fmtDate_(d));
    d.setDate(d.getDate() + 1);
  }
  return dates;
}

function getSelectedYearMonth_() {
  var s = sheet_(SN_OUTPUT);
  if (!s) return null;
  var year = String(s.getRange(YEAR_ROW, YEAR_COL).getValue()).trim();
  var month = String(s.getRange(MONTH_ROW, MONTH_COL).getValue()).trim();
  if (!year || !month) return null;
  return { year: year, month: month };
}

/**
 * Read non-retail layout dynamically from Row 2-3 headers of シフト出力.
 * Row 2: store name headers (merged cells)
 * Row 3: person names (one per 2 columns: 出勤,退勤)
 *
 * Returns: {
 *   stores: { storeName: { names: [name1, name2], cols: [{s,e}, ...] } },
 *   memoCol: N,       // 1-indexed memo column
 *   totalCols: N,     // total number of columns
 *   allPersonCols: [{s,e}, ...]  // flat list of all non-retail person cols
 * }
 */
function readNonRetailLayout_(outputSheet) {
  var lastCol = Math.max(outputSheet.getLastColumn(), NON_RETAIL_START_COL);
  if (lastCol < NON_RETAIL_START_COL) {
    return { stores: {}, storeOrder: [], memoCol: NON_RETAIL_START_COL, totalCols: NON_RETAIL_START_COL, allPersonCols: [] };
  }

  var width = lastCol - NON_RETAIL_START_COL + 1;
  // Row 1 = store headers, Row 2 = person names (after layout change)
  var row2 = outputSheet.getRange(1, NON_RETAIL_START_COL, 1, width).getValues()[0];
  var row3 = outputSheet.getRange(2, NON_RETAIL_START_COL, 1, width).getValues()[0];

  var stores = {};
  var storeOrder = [];
  var currentStore = '';
  var memoCol = lastCol;
  var allPersonCols = [];

  for (var i = 0; i < width; i++) {
    var col1 = NON_RETAIL_START_COL + i; // 1-indexed
    var headerVal = String(row2[i]).trim();
    var nameVal = String(row3[i]).trim();

    // Check if this column is メモ
    if (headerVal === 'メモ') {
      memoCol = col1;
      break;
    }

    // Update current store from Row 2 (merged cells show value only in first cell)
    if (headerVal) {
      currentStore = headerVal;
    }

    // If we have a person name or the sub-header is 出勤, this is a person's start col
    // Person columns come in pairs: 出勤 col, 退勤 col
    // Row 3 has person names on the 出勤 col (odd positions within store section)
    if (currentStore && currentStore !== 'メモ') {
      // Check if this is a 出勤 column (even offset within person pair)
      var row4val = '';
      if (col1 <= lastCol) {
        row4val = String(outputSheet.getRange(3, col1).getValue()).trim();
      }
      if (row4val === '出勤' && col1 + 1 <= lastCol) {
        if (!stores[currentStore]) {
          stores[currentStore] = { names: [], cols: [] };
          storeOrder.push(currentStore);
        }
        var pc = { s: col1, e: col1 + 1 };
        stores[currentStore].names.push(nameVal);
        stores[currentStore].cols.push(pc);
        allPersonCols.push(pc);
        i++; // skip 退勤 column
      }
    }
  }

  return {
    stores: stores,
    storeOrder: storeOrder,
    memoCol: memoCol,
    totalCols: memoCol,
    allPersonCols: allPersonCols,
  };
}

/**
 * Build non-retail layout from staff master data.
 * Groups staff by メイン店舗 for non-retail stores.
 * Returns: {
 *   storeOrder: ['工場', 'EC', '本部オフィス'],
 *   stores: { storeName: { names: [name1, ...], cols: [{s,e}, ...] } },
 *   memoCol: N,
 *   totalCols: N,
 *   allPersonCols: [{s,e}, ...]
 * }
 */
function buildNonRetailLayout_(staff) {
  // Group active staff by メイン店舗 for non-retail stores
  var storeStaff = {};
  staff.forEach(function(s) {
    var mainStore = s['メイン店舗'] || '';
    if (!mainStore) return;
    // Skip retail stores
    if (RETAIL_COLS[mainStore]) return;
    if (!storeStaff[mainStore]) storeStaff[mainStore] = [];
    storeStaff[mainStore].push(s['氏名']);
  });

  // Build ordered store list: known order first, then any extras
  var storeOrder = [];
  NON_RETAIL_STORE_ORDER.forEach(function(name) {
    if (storeStaff[name] && storeStaff[name].length > 0) {
      storeOrder.push(name);
    }
  });
  // Add any stores not in the predefined order
  for (var name in storeStaff) {
    if (NON_RETAIL_STORE_ORDER.indexOf(name) < 0 && storeStaff[name].length > 0) {
      storeOrder.push(name);
    }
  }

  var col = NON_RETAIL_START_COL; // 1-indexed
  var stores = {};
  var allPersonCols = [];

  storeOrder.forEach(function(storeName) {
    var names = storeStaff[storeName];
    var cols = [];
    names.forEach(function(name) {
      var pc = { s: col, e: col + 1 };
      cols.push(pc);
      allPersonCols.push(pc);
      col += 2;
    });
    stores[storeName] = { names: names, cols: cols };
  });

  var memoCol = col;

  return {
    storeOrder: storeOrder,
    stores: stores,
    memoCol: memoCol,
    totalCols: memoCol,
    allPersonCols: allPersonCols,
  };
}

// ==========================================================================
// kintone API
// ==========================================================================

function kintoneAuth_() {
  return Utilities.base64Encode(getProp_('KINTONE_USERNAME') + ':' + getProp_('KINTONE_PASSWORD'));
}

function kintoneGet_(appId, query) {
  var domain = getProp_('KINTONE_DOMAIN');
  var url = 'https://' + domain + '/k/v1/records.json'
    + '?app=' + appId
    + '&query=' + encodeURIComponent(query)
    + '&totalCount=true';
  var resp = UrlFetchApp.fetch(url, {
    method: 'get',
    headers: { 'X-Cybozu-Authorization': kintoneAuth_() },
    muteHttpExceptions: true,
  });
  return JSON.parse(resp.getContentText());
}

function kintonePost_(appId, records) {
  var domain = getProp_('KINTONE_DOMAIN');
  var resp = UrlFetchApp.fetch('https://' + domain + '/k/v1/records.json', {
    method: 'post',
    contentType: 'application/json',
    headers: { 'X-Cybozu-Authorization': kintoneAuth_() },
    payload: JSON.stringify({ app: appId, records: records }),
    muteHttpExceptions: true,
  });
  return JSON.parse(resp.getContentText());
}

function kintonePut_(appId, records) {
  var domain = getProp_('KINTONE_DOMAIN');
  var resp = UrlFetchApp.fetch('https://' + domain + '/k/v1/records.json', {
    method: 'put',
    contentType: 'application/json',
    headers: { 'X-Cybozu-Authorization': kintoneAuth_() },
    payload: JSON.stringify({ app: appId, records: records }),
    muteHttpExceptions: true,
  });
  return JSON.parse(resp.getContentText());
}

function kintoneDelete_(appId, ids) {
  var domain = getProp_('KINTONE_DOMAIN');
  var resp = UrlFetchApp.fetch('https://' + domain + '/k/v1/records.json', {
    method: 'delete',
    contentType: 'application/json',
    headers: { 'X-Cybozu-Authorization': kintoneAuth_() },
    payload: JSON.stringify({ app: appId, ids: ids }),
    muteHttpExceptions: true,
  });
  return JSON.parse(resp.getContentText());
}

/**
 * kintoneから全レコード取得 (ページネーション対応)
 */
function kintoneGetAll_(appId, query) {
  var all = [];
  var offset = 0;
  var limit = 500;
  while (true) {
    var q = query + ' limit ' + limit + ' offset ' + offset;
    var result = kintoneGet_(appId, q);
    var records = result.records || [];
    all = all.concat(records);
    if (records.length < limit) break;
    offset += limit;
  }
  return all;
}

// ==========================================================================
// Slack API
// ==========================================================================

function slackPost_(text, blocks) {
  var token = getProp_('SLACK_BOT_TOKEN');
  var channel = getProp_('SLACK_SHIFT_CHANNEL') || 'C0AKBJ1LTV2';
  if (!token) { Logger.log('[Slack skip] ' + text); return; }
  try {
    var body = { channel: channel, text: text };
    if (blocks) body.blocks = blocks;
    UrlFetchApp.fetch('https://slack.com/api/chat.postMessage', {
      method: 'post',
      contentType: 'application/json',
      headers: { 'Authorization': 'Bearer ' + token },
      payload: JSON.stringify(body),
      muteHttpExceptions: true,
    });
  } catch (e) {
    Logger.log('Slack failed: ' + e.message);
  }
}

function slackError_(location, msg) {
  var now = Utilities.formatDate(new Date(), Session.getScriptTimeZone(), 'yyyy-MM-dd HH:mm:ss');
  slackPost_('【エラー通知】発生箇所:' + location + ' エラー:' + msg + ' 日時:' + now);
}

/**
 * Slack DMで個別にシフト希望を送信する
 * 希望シフト → 日付ごとのボタン付きDM
 * 固定シフト → 希望休入力DM
 * @return {number} 送信したDM数
 */
function sendShiftCollectionDMs_(staff, periodStart, periodEnd, deadline) {
  var token = getProp_('SLACK_BOT_TOKEN');
  if (!token) return 0;

  var sent = 0;

  // 日付リスト生成
  var dates = generateDates_(periodStart, periodEnd);

  staff.forEach(function(s) {
    var slackId = s['Slack ID'];
    if (!slackId) return;
    var staffId = s['staff_id'];
    var workStyle = s['働き方'];

    try {
      if (workStyle === '希望シフト' || workStyle === '混合') {
        // 希望シフト: 日付ごとにボタン付きメッセージ
        var blocks = [
          { type: 'header', text: { type: 'plain_text', text: 'シフト希望入力' } },
          { type: 'section', fields: [
            { type: 'mrkdwn', text: '*対象期間:*\n' + periodStart + ' 〜 ' + periodEnd },
            { type: 'mrkdwn', text: '*回答期限:*\n' + deadline },
          ] },
          { type: 'divider' },
          { type: 'section', text: { type: 'mrkdwn', text: '各日付のボタンを押して出勤/休み/希望休を選んでください。' } },
        ];

        dates.forEach(function(dateStr) {
          var d = new Date(dateStr + 'T00:00:00');
          var dow = DOW_JP[d.getDay()];
          blocks.push({
            type: 'section',
            block_id: 'shift_' + dateStr,
            text: { type: 'mrkdwn', text: '*' + dateStr + '（' + dow + '）*' },
          });
          blocks.push({
            type: 'actions',
            block_id: 'actions_' + dateStr,
            elements: [
              { type: 'button', text: { type: 'plain_text', text: '✅ 出勤' }, style: 'primary', value: staffId, action_id: 'shift_' + dateStr + '_出勤' },
              { type: 'button', text: { type: 'plain_text', text: '🙏 休み希望' }, style: 'danger', value: staffId, action_id: 'shift_' + dateStr + '_希望休' },
            ],
          });
        });

        slackDM_(token, slackId, blocks, '【シフト希望入力】' + periodStart + '〜' + periodEnd);
        sent++;

      } else if (workStyle === '固定シフト') {
        // 固定シフト: 希望休入力
        var countOptions = [];
        for (var n = 1; n <= 8; n++) {
          countOptions.push({ text: { type: 'plain_text', text: n + '日' }, value: staffId + '|' + periodStart + '|' + periodEnd + '|' + n });
        }

        var blocks = [
          { type: 'header', text: { type: 'plain_text', text: '📅 希望休の入力' } },
          { type: 'section', fields: [
            { type: 'mrkdwn', text: '*対象期間:*\n' + periodStart + ' 〜 ' + periodEnd },
            { type: 'mrkdwn', text: '*回答期限:*\n' + deadline },
          ] },
          { type: 'divider' },
          { type: 'section', text: { type: 'mrkdwn', text: '希望休がある場合は日数を選んで日付を入力してください。\n希望休がない場合は「希望休なし」を押してください。' } },
          { type: 'actions', block_id: 'fixed_actions', elements: [
            { type: 'static_select', action_id: 'fixedcount', placeholder: { type: 'plain_text', text: '希望休の日数' }, options: countOptions },
            { type: 'button', text: { type: 'plain_text', text: '希望休なし' }, value: staffId + '|' + periodStart + '|' + periodEnd, action_id: 'fixednone' },
          ] },
        ];

        slackDM_(token, slackId, blocks, '【希望休入力】' + periodStart + '〜' + periodEnd);
        sent++;
      }
    } catch (e) {
      Logger.log('DM send failed for ' + s['氏名'] + ': ' + e.message);
    }
  });

  return sent;
}

/**
 * Slack DMを送信する (chat.postMessage to user ID)
 */
function slackDM_(token, userId, blocks, fallbackText) {
  UrlFetchApp.fetch('https://slack.com/api/chat.postMessage', {
    method: 'post',
    contentType: 'application/json',
    headers: { 'Authorization': 'Bearer ' + token },
    payload: JSON.stringify({
      channel: userId,
      blocks: blocks,
      text: fallbackText,
    }),
    muteHttpExceptions: true,
  });
}


// ==========================================================================
// Sheet reader helpers
// ==========================================================================

function readStaffMaster_() {
  var rows = sheetData_(SN_STAFF);
  if (rows.length < 2) return [];
  var header = rows[0];
  var storeNames = ['藤沢', '伊勢佐木町', '新宿', '工場', '本部オフィス', 'EC'];
  var staff = [];
  for (var r = 1; r < rows.length; r++) {
    if (!rows[r][0]) continue;
    var d = {};
    for (var c = 0; c < header.length; c++) {
      d[String(header[c])] = String(rows[r][c] || '');
    }
    var assignedStores = storeNames.filter(function(name) {
      return d[name] === 'TRUE' || d[name] === 'true';
    });
    d['対応店舗'] = assignedStores.join(', ');
    if (d['有効フラグ'] === '無効') continue;
    staff.push(d);
  }
  return staff;
}

function readStoreMaster_() {
  var rows = sheetData_(SN_STORE);
  if (rows.length < 2) return [];
  var header = rows[0];
  var stores = [];
  for (var r = 1; r < rows.length; r++) {
    if (!rows[r][0]) continue;
    var d = {};
    for (var c = 0; c < header.length; c++) {
      d[String(header[c])] = String(rows[r][c] || '');
    }
    if (d['有効フラグ'] === '無効') continue;
    stores.push(d);
  }
  return stores;
}

function readRulesMaster_() {
  var rows = sheetData_(SN_RULES);
  var rules = [];
  for (var r = 2; r < rows.length; r++) {
    var val = String(rows[r][0] || '').trim();
    if (val) rules.push(val);
  }
  return rules;
}

// ==========================================================================
// ① スタッフマスタ同期
// ==========================================================================

function syncStaffMaster() {
  ensureTrigger_();
  var ui = SpreadsheetApp.getUi();
  try {
    var staff = readStaffMaster_();
    if (!staff.length) { ui.alert('スタッフマスタにデータがありません'); return; }

    // --- 希望収集データのヘッダー更新 ---
    // Layout: Row 1=年月セレクター(触らない), Row 2=スタッフ名, Row 3=サブヘッダー, Row 4+=データ(触らない)
    // Left: 希望シフト (2列/人: 出勤+退勤), Right: 固定シフト (1列/人: 希望休)
    var wishStaffCount = updateWishSheetHeaders_(staff);

    // ヘッダー再構築後、希望データから表示中の月を再描画 (デザイン・プルダウン・週末色を完全復元)
    var wishSheet0 = sheet_(SN_WISH);
    if (wishSheet0) {
      var wy = String(wishSheet0.getRange(1, 1).getValue()).trim();
      var wm = String(wishSheet0.getRange(2, 1).getValue()).trim();
      if (wy && wm) {
        try { loadWishSheetData_(wy, wm); } catch (we) { Logger.log('wish reload: ' + we.message); }
      }
    }

    // --- シフト出力の非営業セクション + 名前ドロップダウン更新 ---
    var outputResult = updateShiftOutputLayout_(staff);

    // レイアウト更新後、現在の年月データを再読込（非営業セクションのデータ復元）
    var ym = getSelectedYearMonth_();
    var reloadMsg = '';
    if (ym) {
      reloadMsg = loadShiftOutputData_(ym.year, ym.month);
    }

    // --- 個人シフトのセレクター/ドロップダウンを最新化 ---
    ensurePersonalSheet_();

    ui.alert(
      'スタッフマスタ同期完了\n\n' +
      '希望収集データ: ' + wishStaffCount + '名のヘッダーを更新\n' +
      'シフト出力: ' + outputResult.message + (reloadMsg ? ' → データ再読込: ' + reloadMsg : '')
    );
  } catch (e) {
    slackError_('syncStaffMaster', e.message);
    ui.alert('エラー: ' + e.message);
  }
}

// ==========================================================================
// 希望収集データ ヘッダー更新
// ==========================================================================

/**
 * 希望収集データシートの Row 2 (スタッフ名) と Row 3 (サブヘッダー) を更新する。
 * Row 1 (年月セレクター) と Row 4+ (データ) は一切触らない。
 *
 * レイアウト:
 *   A2="", B2="" (固定列)
 *   C2~ : 希望シフトスタッフ名 (2列結合/人)
 *   その右: 固定シフトスタッフ名 (1列/人)
 *   A3="日付", B3="曜日"
 *   C3~ : 出勤/退勤 (希望) | 希望休 (固定)
 */
function updateWishSheetHeaders_(staff) {
  var wishSheet = sheet_(SN_WISH);
  if (!wishSheet) return 0;

  // 並び順 (2026-07-03 ユーザー指定):
  //   左 = 営業店舗スタッフの希望ペア → 固定シフト組(希望休) → 工場など非営業の希望ペア(牧田等)
  var isWish = function(s) { return s['働き方'] === '希望シフト' || s['働き方'] === '混合'; };
  var retailWish = staff.filter(function(s) { return isWish(s) && !!RETAIL_COLS[s['メイン店舗']]; });
  var nonRetailWish = staff.filter(function(s) { return isWish(s) && !RETAIL_COLS[s['メイン店舗']]; });
  var fixedStaff = staff.filter(function(s) { return s['働き方'] === '固定シフト'; });

  // ブロック列定義: [ {name, type:'pair'|'single'} ... ]
  var blocks = [];
  retailWish.forEach(function(s) { blocks.push({ name: s['氏名'], type: 'pair' }); });
  fixedStaff.forEach(function(s) { blocks.push({ name: s['氏名'], type: 'single' }); });
  nonRetailWish.forEach(function(s) { blocks.push({ name: s['氏名'], type: 'pair' }); });

  var totalDataCols = 0;
  blocks.forEach(function(b) { totalDataCols += (b.type === 'pair') ? 2 : 1; });
  var totalCols = 2 + totalDataCols; // A,B + data
  var nWish = retailWish.length + nonRetailWish.length;
  var nFixed = fixedStaff.length;

  // -- Step 1: ヘッダー行(2-3)を全幅リセット + 増減で余った旧列のデータ領域も掃除 (冪等・崩れ根絶) --
  var maxCol = wishSheet.getMaxColumns();
  if (maxCol > 2) {
    var r23 = wishSheet.getRange(2, 3, 2, maxCol - 2);
    r23.breakApart();
    r23.clearContent();
    r23.clearFormat();
  }
  if (maxCol > totalCols) {
    // 余剰列: データ領域の値・プルダウン・背景を除去
    var stale = wishSheet.getRange(4, totalCols + 1, 31, maxCol - totalCols);
    stale.clearContent();
    stale.clearDataValidations();
    stale.setBackground(null);
  }

  // -- Step 2-3: Row2(名前) / Row3(サブヘッダー) を構築 --
  var row2vals = [];
  var row3vals = [];
  blocks.forEach(function(b) {
    if (b.type === 'pair') {
      row2vals.push(b.name, '');
      row3vals.push('出勤', '退勤');
    } else {
      row2vals.push(b.name);
      row3vals.push('希望休');
    }
  });
  if (row2vals.length > 0) {
    wishSheet.getRange(2, 3, 1, row2vals.length).setValues([row2vals]);
    wishSheet.getRange(3, 3, 1, row3vals.length).setValues([row3vals]);
  }

  // -- Step 4: ペアの2列結合 --
  var col = 3;
  blocks.forEach(function(b) {
    if (b.type === 'pair') {
      wishSheet.getRange(2, col, 1, 2).merge();
      col += 2;
    } else {
      col += 1;
    }
  });

  // -- Step 5: 固定ラベル (A2=月セレクター値は消さない) --
  wishSheet.getRange(3, 1).setValue('日付');
  wishSheet.getRange(3, 2).setValue('曜日');

  // -- Step 6: スタイリング (他シートと統一の配色) --
  wishSheet.getRange(2, 3, 1, Math.max(1, totalCols - 2))
    .setFontWeight('bold').setHorizontalAlignment('center').setVerticalAlignment('middle').setFontSize(10);
  // ブロック別の背景: 営業ペア=青 / 固定=緑 / 非営業ペア=黄
  col = 3;
  blocks.forEach(function(b) {
    var w = (b.type === 'pair') ? 2 : 1;
    var idx = blocks.indexOf(b);
    var isRetailPair = idx < retailWish.length;
    var bg = (b.type === 'single') ? '#D9EAD3' : (isRetailPair ? '#D9EBF7' : '#FCE8B2');
    wishSheet.getRange(2, col, 1, w).setBackground(bg);
    col += w;
  });
  wishSheet.getRange(3, 1, 1, totalCols)
    .setFontWeight('bold').setHorizontalAlignment('center').setVerticalAlignment('middle')
    .setFontSize(8).setBackground('#E8EAED');

  // 年月セレクター統一デザイン (行2/3の塗りより後に適用して上書きを防ぐ)
  // 旧レイアウトの A1:B1 / A2:B2 結合を解除 (結合中はB列ラベルが書けない)
  wishSheet.getRange(1, 1, 2, 2).breakApart();
  styleYmSelector_(wishSheet, 2);

  // -- Step 7: セル幅・固定表示・ブロック区切り罫線 --
  try {
    wishSheet.setColumnWidth(1, 56);  // 日付 (年セレクター14ptが収まる幅)
    wishSheet.setColumnWidth(2, 40);  // 曜日
    col = 3;
    blocks.forEach(function(b) {
      if (b.type === 'pair') {
        wishSheet.setColumnWidth(col, 64);
        wishSheet.setColumnWidth(col + 1, 64);
        col += 2;
      } else {
        wishSheet.setColumnWidth(col, 88);
        col += 1;
      }
    });
    wishSheet.setFrozenRows(3);
    wishSheet.setFrozenColumns(2);
  } catch (e) { Logger.log('wish width: ' + e.message); }

  // ブロック右端に区切り罫線 (ヘッダー+データ領域)
  col = 3;
  blocks.forEach(function(b) {
    var w = (b.type === 'pair') ? 2 : 1;
    wishSheet.getRange(2, col + w - 1, 33, 1)
      .setBorder(null, null, null, true, null, null, '#9AA0A6', SpreadsheetApp.BorderStyle.SOLID);
    col += w;
  });

  Logger.log('updateWishSheetHeaders_: retail=' + retailWish.length + ', fixed=' + nFixed + ', nonRetail=' + nonRetailWish.length + ', totalCols=' + totalCols);
  return nWish + nFixed;
}

// ==========================================================================
// シフト出力 名前ドロップダウン更新 (横型レイアウト)
// ==========================================================================

/**
 * シフト出力シートの非営業セクションを再構築し、名前ドロップダウンを更新する。
 * ① syncStaffMaster から呼ばれる。
 */
// 年月セレクターの統一デザイン (3シート共通: シフト作成の濃オレンジが正)
// A1=年値 / A2=月値、labelCol に「年」「月」ラベル (シフト作成=C列、希望シフト・個人シフト=B列)
function styleYmSelector_(sh, labelCol) {
  sh.getRange(1, 1, 2, 1)
    .setBackground('#F2A500').setFontWeight('bold').setFontSize(14)
    .setHorizontalAlignment('center').setVerticalAlignment('middle');
  if (labelCol) {
    sh.getRange(1, labelCol).setValue('年');
    sh.getRange(2, labelCol).setValue('月');
    sh.getRange(1, labelCol, 2, 1)
      .setBackground('#F2A500').setFontWeight('bold').setFontSize(11)
      .setHorizontalAlignment('center').setVerticalAlignment('middle');
  }
}

function updateShiftOutputLayout_(staff) {
  var outputSheet = sheet_(SN_OUTPUT);
  if (!outputSheet) return { message: 'シート未検出' };

  // Build new layout from staff master
  var newLayout = buildNonRetailLayout_(staff);

  // Read current layout to determine what needs clearing
  var oldLayout = readNonRetailLayout_(outputSheet);
  var oldTotalCols = oldLayout.totalCols;
  var newTotalCols = newLayout.totalCols;

  // Clear old non-retail section (rows 1-3 headers + data area)
  var clearWidth = Math.max(oldTotalCols, newTotalCols) - NON_RETAIL_START_COL + 2;
  if (clearWidth > 0) {
    // Clear headers (rows 1-3): unmerge + clear values only
    var headerRange = outputSheet.getRange(1, NON_RETAIL_START_COL, 3, clearWidth);
    headerRange.breakApart();
    headerRange.clearContent();

    // Clear data area (values + validations only, preserve formatting)
    var dataRange = outputSheet.getRange(OUTPUT_DSTART, NON_RETAIL_START_COL, OUTPUT_DAYS * 2, clearWidth);
    dataRange.clearContent();
    dataRange.clearDataValidations();
  }

  // Write new Row 1 (store name headers with merges)
  newLayout.storeOrder.forEach(function(storeName) {
    var storeInfo = newLayout.stores[storeName];
    if (!storeInfo.cols.length) return;
    var firstCol = storeInfo.cols[0].s;
    var lastCol = storeInfo.cols[storeInfo.cols.length - 1].e;
    var span = lastCol - firstCol + 1;

    outputSheet.getRange(1, firstCol).setValue(storeName);
    if (span > 1) {
      outputSheet.getRange(1, firstCol, 1, span).merge();
    }
  });

  // Write メモ header
  outputSheet.getRange(1, newLayout.memoCol).setValue('メモ');

  // Write new Row 2 (person names with merge)
  for (var storeName in newLayout.stores) {
    var storeInfo = newLayout.stores[storeName];
    for (var i = 0; i < storeInfo.cols.length; i++) {
      outputSheet.getRange(2, storeInfo.cols[i].s).setValue(storeInfo.names[i]);
      outputSheet.getRange(2, storeInfo.cols[i].s, 1, 2).merge();
    }
  }

  // Write new Row 3 (出勤/退勤 sub-headers)
  for (var storeName in newLayout.stores) {
    var storeInfo = newLayout.stores[storeName];
    for (var i = 0; i < storeInfo.cols.length; i++) {
      outputSheet.getRange(3, storeInfo.cols[i].s).setValue('出勤');
      outputSheet.getRange(3, storeInfo.cols[i].e).setValue('退勤');
    }
  }
  outputSheet.getRange(3, newLayout.memoCol).setValue('メモ');

  // NOTE: ヘッダーの色・フォント・枠線等の見た目は手動設定を維持。
  // ここでは値とセル結合のみ設定する。

  // --- Retail name dropdowns ---
  var activeNames = staff.map(function(s) { return s['氏名']; });
  activeNames.unshift('');
  var rule = SpreadsheetApp.newDataValidation()
    .requireValueInList(activeNames, true)
    .setAllowInvalid(true)
    .build();

  var dropdownCount = 0;
  for (var d = 0; d < OUTPUT_DAYS; d++) {
    var nameRowNum = OUTPUT_DSTART + d * 2;
    for (var store in RETAIL_COLS) {
      var shifts = RETAIL_COLS[store];
      for (var st = 0; st < SHIFT_TYPES.length; st++) {
        var cols = shifts[SHIFT_TYPES[st]];
        outputSheet.getRange(nameRowNum, cols.n).setDataValidation(rule);
        dropdownCount++;
      }
    }
  }

  styleYmSelector_(outputSheet, 3);

  var personCount = newLayout.allPersonCols.length;
  Logger.log('updateShiftOutputLayout_: ' + newLayout.storeOrder.length + ' stores, '
    + personCount + ' persons, totalCols=' + newTotalCols);

  return {
    message: '非営業 ' + personCount + '名 (' + newLayout.storeOrder.join('/') + '), ドロップダウン ' + dropdownCount + '箇所更新',
  };
}

// ==========================================================================
// ② 希望シフト取得
// ==========================================================================

function fetchWishData() {
  ensureTrigger_();
  var ui = SpreadsheetApp.getUi();
  try {
    var ym = getSelectedYearMonth_();
    if (!ym) { ui.alert('年月セレクター(シフト出力 A1=年/A2=月)を設定してください'); return; }

    var year = ym.year;
    var month = ym.month;
    var m = ('0' + month).slice(-2);
    var firstDate = year + '-' + m + '-01';
    var lastDay = new Date(parseInt(year), parseInt(month), 0).getDate();
    var lastDate = year + '-' + m + '-' + ('0' + lastDay).slice(-2);

    // Sync wish sheet year/month (A1=年, A2=月)
    var wishSheet = sheet_(SN_WISH);
    if (wishSheet) {
      wishSheet.getRange(1, 1).setValue(parseInt(year));   // A1=年
      wishSheet.getRange(2, 1).setValue(parseInt(month));  // A2=月
    }

    var msg = loadWishSheetData_(year, month);

    ui.alert(
      '希望シフト取得完了\n\n' +
      '対象期間: ' + firstDate + ' ~ ' + lastDate + '\n' +
      msg
    );
  } catch (e) {
    slackError_('fetchWishData', e.message);
    ui.alert('エラー: ' + e.message);
  }
}

// ==========================================================================
// ③ AIシフト生成
// ==========================================================================

function generateShift() {
  ensureTrigger_();
  var ui = SpreadsheetApp.getUi();

  try {
    var ym = getSelectedYearMonth_();
    if (!ym) { ui.alert('年月セレクター(A1/A2)を設定してください'); return; }
    var year = ym.year;
    var month = ym.month;
    var m = ('0' + month).slice(-2);
    var lastDay = new Date(parseInt(year), parseInt(month), 0).getDate();

    // 期間選択ダイアログ (HTML)
    var html = HtmlService.createHtmlOutput(
      '<style>'
      + 'body { font-family: sans-serif; padding: 16px; }'
      + 'label { display: block; margin: 8px 0 4px; font-weight: bold; }'
      + 'select { font-size: 14px; padding: 4px 8px; }'
      + '.btn { margin-top: 16px; padding: 8px 24px; font-size: 14px; cursor: pointer; }'
      + '.btn-ok { background: #4285f4; color: white; border: none; border-radius: 4px; }'
      + '.btn-cancel { background: #eee; border: 1px solid #ccc; border-radius: 4px; margin-left: 8px; }'
      + '</style>'
      + '<p>' + year + '年' + month + '月のシフト生成期間を選択してください</p>'
      + '<label>開始日</label>'
      + '<select id="startDay">'
      + buildDayOptions_(1, lastDay)
      + '</select>'
      + '<label>終了日</label>'
      + '<select id="endDay">'
      + buildDayOptions_(lastDay, lastDay)
      + '</select>'
      + '<div style="margin-top:16px">'
      + '<button class="btn btn-ok" onclick="submit()">生成開始</button>'
      + '<button class="btn btn-cancel" onclick="google.script.host.close()">キャンセル</button>'
      + '</div>'
      + '<script>'
      + 'function submit(){'
      + '  var s = document.getElementById("startDay").value;'
      + '  var e = document.getElementById("endDay").value;'
      + '  if (parseInt(s) > parseInt(e)) { alert("開始日は終了日以前にしてください"); return; }'
      + '  google.script.run.withSuccessHandler(function(){ google.script.host.close(); })'
      + '    .withFailureHandler(function(err){ alert("エラー: " + err.message); google.script.host.close(); })'
      + '    .generateShiftForPeriod(s, e);'
      + '}'
      + '</script>'
    ).setWidth(320).setHeight(260);

    ui.showModalDialog(html, '③ AIシフト生成 - 期間選択');
  } catch (e) {
    slackError_('generateShift', e.message);
    ui.alert('エラー: ' + e.message);
  }
}

function buildDayOptions_(defaultDay, lastDay) {
  var opts = '';
  for (var d = 1; d <= lastDay; d++) {
    opts += '<option value="' + d + '"' + (d === defaultDay ? ' selected' : '') + '>' + d + '日</option>';
  }
  return opts;
}

/**
 * ダイアログから呼ばれる: 指定期間のシフトをAI生成 (バリデーション+再生成ループ付き)
 */
function generateShiftForPeriod(startDay, endDay) {
  var MAX_RETRIES = 3;
  var ym = getSelectedYearMonth_();
  var year = ym.year;
  var month = ym.month;
  var m = ('0' + month).slice(-2);
  var firstDate = year + '-' + m + '-' + ('0' + parseInt(startDay)).slice(-2);
  var lastDate = year + '-' + m + '-' + ('0' + parseInt(endDay)).slice(-2);

  var staff = readStaffMaster_();
  var stores = readStoreMaster_();
  var rules = readRulesMaster_();
  if (!staff.length) throw new Error('スタッフマスタが空です');

  var wishes = readWishesFromSheet_(staff, year);
  var dates = generateDates_(firstDate, lastDate);
  var prompt = buildClaudePrompt_(staff, stores, rules, wishes, dates);

  var result = null;
  var lastErrors = [];

  for (var attempt = 1; attempt <= MAX_RETRIES; attempt++) {
    var callPrompt = prompt;
    if (attempt > 1 && lastErrors.length > 0) {
      callPrompt += '\n\n## 前回の生成結果に以下の問題がありました。修正してください:\n'
        + lastErrors.map(function(e) { return '- ' + e; }).join('\n');
    }

    var response = callGeminiApi_(callPrompt);
    result = parseAiResponse_(response);

    var errors = validateShiftResult_(result, staff, stores, wishes, dates);
    if (errors.length === 0) {
      Logger.log('Shift generation passed validation on attempt ' + attempt);
      break;
    }

    Logger.log('Attempt ' + attempt + ' failed validation: ' + errors.length + ' errors');
    lastErrors = errors;

    if (attempt === MAX_RETRIES) {
      Logger.log('Max retries reached. Using last result with ' + errors.length + ' remaining issues.');
    }
  }

  writeShiftOutput_(result, staff, stores, dates);

  // AI出力を一時保存 (④実行時に人間の修正と比較するため)
  saveAiOutput_(result, dates);

  var statusMsg = lastErrors.length > 0
    ? ' (注意: ' + lastErrors.length + '件の問題が残っています。手動確認してください)'
    : '';

  slackPost_(
    '*シフト自動生成完了*\n' +
    '対象期間: ' + firstDate + ' ~ ' + lastDate + '\n' +
    'スタッフ数: ' + staff.length + '名' + statusMsg + '\n' +
    'スプレッドシートの「シフト出力」シートを確認してください。'
  );
}

/**
 * 生成結果のバリデーション
 * @return {string[]} エラーメッセージの配列 (空=問題なし)
 */
function validateShiftResult_(result, staff, stores, wishes, dates) {
  var errors = [];
  var schedule = result.schedule || [];

  // スタッフ名→情報のマップ
  var staffMap = {};
  staff.forEach(function(s) { staffMap[s['氏名']] = s; });

  // staff_id→名前のマップ
  var sidToName = {};
  staff.forEach(function(s) { sidToName[s['staff_id']] = s['氏名']; });

  var retailStoreNames = Object.keys(RETAIL_COLS);

  // 日付→スケジュールのマップ
  var dayMap = {};
  schedule.forEach(function(d) { dayMap[d.date] = d; });

  // 週ごとの労働時間集計用
  var weeklyHours = {}; // { staffName: { weekKey: hours } }

  // --- Check 1: 全日分のデータがあるか ---
  dates.forEach(function(dateStr) {
    if (!dayMap[dateStr]) {
      errors.push(dateStr + 'のスケジュールが欠落しています');
    }
  });

  // --- Check 2-5: 各日のチェック ---
  dates.forEach(function(dateStr) {
    var day = dayMap[dateStr];
    if (!day) return;
    var storeAssigns = day.stores || {};
    var times = day.times || {};

    // Check 2: 営業店舗に早番/遅番が配置されているか
    retailStoreNames.forEach(function(storeName) {
      var assign = storeAssigns[storeName];
      if (!assign) {
        errors.push(dateStr + ': ' + storeName + 'の配置がありません');
        return;
      }
      if (!assign['早番']) errors.push(dateStr + ': ' + storeName + 'の早番が未配置');
      if (!assign['遅番']) errors.push(dateStr + ': ' + storeName + 'の遅番が未配置');
    });

    // Check 2.5: 店舗マスタ「最低必要人数/日」による店舗別人員チェック
    stores.forEach(function(s) {
      var storeName = s['店舗名'];
      if (!storeName) return;
      var minReq = parseInt(s['最低必要人数/日'] || '0', 10) || 0;
      if (minReq <= 0) return;
      var assign = storeAssigns[storeName];
      var count = 0;
      if (Array.isArray(assign)) {
        count = assign.filter(function(n) { return n; }).length;
      } else if (assign && typeof assign === 'object') {
        for (var k in assign) { if (assign[k]) count++; }
      }
      if (count < minReq) {
        errors.push(dateStr + ' ' + storeName + ': 配置' + count + '人 (最低' + minReq + '人必要)');
      }
    });

    // Check 3: 希望休が尊重されているか
    var allAssigned = {};
    for (var sn in storeAssigns) {
      var assign = storeAssigns[sn];
      if (Array.isArray(assign)) {
        assign.forEach(function(name) { allAssigned[name] = sn; });
      } else if (typeof assign === 'object') {
        for (var key in assign) {
          if (assign[key]) allAssigned[assign[key]] = sn;
        }
      }
    }

    staff.forEach(function(s) {
      var sid = s['staff_id'];
      var name = s['氏名'];
      var sw = wishes[sid];
      if (!sw || !sw[dateStr]) return;
      if (sw[dateStr].shift_type === '希望休' && allAssigned[name]) {
        errors.push(dateStr + ': ' + name + 'は希望休なのに' + allAssigned[name] + 'に配置されています');
      }
    });

    // Check 4: 対応店舗外への配置
    for (var name in allAssigned) {
      var info = staffMap[name];
      if (!info) {
        errors.push(dateStr + ': 不明なスタッフ名 "' + name + '"');
        continue;
      }
      var assignedStore = allAssigned[name];
      var allowedStores = (info['対応店舗'] || '').split(', ').filter(function(s) { return s; });
      if (allowedStores.length > 0 && allowedStores.indexOf(assignedStore) < 0) {
        errors.push(dateStr + ': ' + name + 'は' + assignedStore + 'に対応していません (対応: ' + allowedStores.join(',') + ')');
      }
    }

    // Check 5: 労働時間集計
    for (var name in times) {
      var t = times[name];
      if (!t || t.indexOf('-') < 0) continue;
      var parts = t.split('-');
      var startH = parseFloat(parts[0].replace(':', '.'));
      var endH = parseFloat(parts[1].replace(':', '.'));
      // 分を正しく計算
      var startParts = parts[0].split(':');
      var endParts = parts[1].split(':');
      var startMin = parseInt(startParts[0]) * 60 + parseInt(startParts[1] || 0);
      var endMin = parseInt(endParts[0]) * 60 + parseInt(endParts[1] || 0);
      if (endMin <= startMin) endMin += 24 * 60; // 日をまたぐ
      var hours = (endMin - startMin) / 60;

      // 週の計算 (ISO week)
      var d = new Date(dateStr + 'T00:00:00');
      var weekStart = new Date(d);
      weekStart.setDate(d.getDate() - d.getDay()); // 日曜始まり
      var weekKey = Utilities.formatDate(weekStart, Session.getScriptTimeZone(), 'yyyy-MM-dd');

      if (!weeklyHours[name]) weeklyHours[name] = {};
      if (!weeklyHours[name][weekKey]) weeklyHours[name][weekKey] = 0;
      weeklyHours[name][weekKey] += hours;
    }
  });

  // Check 5 continued: 正社員・固定シフトのみ月45時間残業上限チェック (36協定)
  // 役員は自動生成対象外、アルバイトは希望シフトベース
  var monthlyOvertime = {}; // { name: total overtime hours }

  for (var name in weeklyHours) {
    var info = staffMap[name];
    if (!info) continue;
    if (info['雇用形態'] !== '正社員') continue;
    if (info['働き方'] !== '固定シフト') continue;
    var weeks = weeklyHours[name];
    for (var weekKey in weeks) {
      var overtime = Math.max(0, weeks[weekKey] - 40);
      if (!monthlyOvertime[name]) monthlyOvertime[name] = 0;
      monthlyOvertime[name] += overtime;
    }
  }

  for (var name in monthlyOvertime) {
    if (monthlyOvertime[name] > 45) {
      errors.push(name + ': 月の時間外労働が' + Math.round(monthlyOvertime[name] * 10) / 10 + 'h (36協定上限45h超過)');
    }
  }

  // 1日8時間超の日が多すぎる場合の警告 (特定スタッフへの偏り)
  var dailyOvertimeCount = {}; // { name: 8h超の日数 }
  dates.forEach(function(dateStr) {
    var day = dayMap[dateStr];
    if (!day) return;
    var times = day.times || {};
    for (var name in times) {
      var t = times[name];
      if (!t || t.indexOf('-') < 0) continue;
      var parts = t.split('-');
      var sp = parts[0].split(':'), ep = parts[1].split(':');
      var sm = parseInt(sp[0]) * 60 + parseInt(sp[1] || 0);
      var em = parseInt(ep[0]) * 60 + parseInt(ep[1] || 0);
      if (em <= sm) em += 24 * 60;
      var hours = (em - sm) / 60;
      if (hours > 8) {
        if (!dailyOvertimeCount[name]) dailyOvertimeCount[name] = 0;
        dailyOvertimeCount[name]++;
      }
    }
  });
  // 期間の半分以上の日で8h超なら警告
  var halfDays = Math.ceil(dates.length / 2);
  for (var name in dailyOvertimeCount) {
    if (dailyOvertimeCount[name] > halfDays) {
      errors.push(name + ': ' + dates.length + '日中' + dailyOvertimeCount[name] + '日で8時間超勤務（残業の偏り）');
    }
  }

  // Check 6: 勤務間インターバル (11時間)
  for (var di = 0; di < dates.length - 1; di++) {
    var today = dayMap[dates[di]];
    var tomorrow = dayMap[dates[di + 1]];
    if (!today || !tomorrow) continue;
    var todayTimes = today.times || {};
    var tomorrowTimes = tomorrow.times || {};

    // 明日出勤するスタッフの今日の終了時間をチェック
    for (var name in tomorrowTimes) {
      if (!todayTimes[name]) continue;
      var todayT = todayTimes[name];
      var tomorrowT = tomorrowTimes[name];
      if (!todayT || !tomorrowT) continue;

      var todayEnd = todayT.split('-')[1];
      var tomorrowStart = tomorrowT.split('-')[0];
      if (!todayEnd || !tomorrowStart) continue;

      var endParts = todayEnd.split(':');
      var startParts = tomorrowStart.split(':');
      var endMin = parseInt(endParts[0]) * 60 + parseInt(endParts[1] || 0);
      var startMin = parseInt(startParts[0]) * 60 + parseInt(startParts[1] || 0);

      // 日をまたぐ終了時間 (0:30 = 翌日0:30)
      if (endMin < 6 * 60) endMin += 24 * 60; // 6時前なら前日深夜
      var interval = (24 * 60 - endMin) + startMin;
      if (interval < 11 * 60) {
        errors.push(dates[di] + '→' + dates[di + 1] + ': ' + name + 'の勤務間インターバルが' + Math.round(interval / 60 * 10) / 10 + '時間 (11時間未満)');
      }
    }
  }

  // Check 7: 連続勤務 (6日以内)
  var staffDates = {}; // { name: [dateStr, ...] }
  dates.forEach(function(dateStr) {
    var day = dayMap[dateStr];
    if (!day) return;
    var times = day.times || {};
    for (var name in times) {
      if (!staffDates[name]) staffDates[name] = [];
      staffDates[name].push(dateStr);
    }
  });
  for (var name in staffDates) {
    var sDates = staffDates[name].sort();
    var consecutive = 1;
    for (var i = 1; i < sDates.length; i++) {
      var prev = new Date(sDates[i - 1] + 'T00:00:00');
      var curr = new Date(sDates[i] + 'T00:00:00');
      var diffDays = Math.round((curr - prev) / (1000 * 60 * 60 * 24));
      if (diffDays === 1) {
        consecutive++;
        if (consecutive > 6) {
          errors.push(name + ': ' + sDates[i - 6] + '〜' + sDates[i] + 'で7日以上連続勤務');
          break;
        }
      } else {
        consecutive = 1;
      }
    }
  }

  // エラー数が多すぎる場合は先頭20件に制限 (再生成プロンプトが長くなりすぎるため)
  if (errors.length > 20) {
    var total = errors.length;
    errors = errors.slice(0, 20);
    errors.push('...他 ' + (total - 20) + '件のエラー');
  }

  return errors;
}

function readWishesFromSheet_(staff, year) {
  var rows = sheetData_(SN_WISH);
  if (rows.length < 4) return {};  // Need at least Row 1(selector), Row 2(names), Row 3(sub), Row 4+(data)

  var nameRow = rows[1];   // Row 2: staff names
  var subRow = rows[2];    // Row 3: sub-headers (出勤/退勤/希望休)
  var nameToSid = {};
  staff.forEach(function(s) { nameToSid[s['氏名']] = s['staff_id']; });

  // Parse wish staff columns (出勤/退勤 pairs)
  var wishCols = [];  // [{name, col}] - col is 0-indexed
  for (var c = 2; c < nameRow.length; c++) {
    var name = String(nameRow[c]).trim();
    var sub = String(subRow[c]).trim();
    if (name && sub === '出勤') {
      wishCols.push({ name: name, col: c });
    }
  }

  // Parse fixed staff columns (希望休)
  var fixedCols = [];
  for (var c = 2; c < nameRow.length; c++) {
    var name = String(nameRow[c]).trim();
    var sub = String(subRow[c]).trim();
    if (name && sub === '希望休') {
      fixedCols.push({ name: name, col: c });
    }
  }

  var wishes = {};

  // Data starts at Row 4 (index 3)
  for (var r = 3; r < rows.length; r++) {
    var dateLabel = String(rows[r][0]).trim();
    if (!dateLabel || dateLabel.indexOf('/') < 0) continue;
    var parts = dateLabel.split('/');
    var dateStr = year + '-' + ('0' + parseInt(parts[0])).slice(-2) + '-' + ('0' + parseInt(parts[1])).slice(-2);

    // Wish staff (出勤/退勤)
    for (var si = 0; si < wishCols.length; si++) {
      var wc = wishCols[si];
      var sid = nameToSid[wc.name];
      if (!sid) continue;

      var val1 = String(rows[r][wc.col] || '').trim();
      var val2 = String(rows[r][wc.col + 1] || '').trim();

      if (!val1 && !val2) continue;
      if (!wishes[sid]) wishes[sid] = {};

      if (val1 === '×') {
        wishes[sid][dateStr] = { shift_type: '希望休', start_time: '', end_time: '' };
      } else if (val1 || val2) {
        wishes[sid][dateStr] = { shift_type: '出勤', start_time: val1, end_time: val2 };
      }
    }

    // Fixed staff (希望休)
    for (var si = 0; si < fixedCols.length; si++) {
      var fc = fixedCols[si];
      var sid = nameToSid[fc.name];
      if (!sid) continue;

      var val = String(rows[r][fc.col] || '').trim();
      if (val === '希望休') {
        if (!wishes[sid]) wishes[sid] = {};
        wishes[sid][dateStr] = { shift_type: '希望休', start_time: '', end_time: '' };
      }
    }
  }
  return wishes;
}

function buildClaudePrompt_(staff, stores, rules, wishes, dates) {
  var retailStores = stores.filter(function(s) { return s['種別'] === '営業'; });
  var otherStores = stores.filter(function(s) { return s['種別'] !== '営業'; });

  // 役員は自動生成対象外
  staff = staff.filter(function(s) { return s['雇用形態'] !== '役員'; });

  var staffInfo = staff.map(function(s) {
    return '- ' + s['氏名'] + ' (雇用:' + s['雇用形態'] + ', 働き方:' + s['働き方']
      + ', 役職:' + s['役職']
      + ', 対応店舗:' + (s['対応店舗'] || '全店舗')
      + (s['個人ルール'] ? ', ルール:' + s['個人ルール'] : '') + ')';
  });

  var wishLines = [];
  staff.forEach(function(s) {
    var sid = s['staff_id'];
    var sw = wishes[sid];
    if (!sw) return;
    var entries = [];
    dates.forEach(function(dateStr) {
      var day = sw[dateStr];
      if (!day) return;
      var d = new Date(dateStr + 'T00:00:00');
      var dl = (d.getMonth() + 1) + '/' + d.getDate();
      if (day.shift_type === '希望休') {
        entries.push(dl + ':×');
      } else if (day.start_time && day.end_time) {
        entries.push(dl + ':' + day.start_time + '-' + day.end_time);
      }
    });
    if (entries.length) wishLines.push('- ' + s['氏名'] + ': ' + entries.join(', '));
  });

  var storeInfo = [];
  retailStores.forEach(function(s) {
    var hours = (s['勤務開始'] && s['勤務開始'] !== '×') ? ' 営業時間 ' + s['勤務開始'] + '〜' + s['勤務終了'] + '、' : '';
    storeInfo.push('- ' + s['店舗名'] + ': 営業店舗、' + hours + '早番/遅番の2人体制、最低' + (s['最低必要人数/日'] || '2') + '人/日。勤務時間は必ず営業時間の範囲内にすること');
  });
  otherStores.forEach(function(s) {
    storeInfo.push('- ' + s['店舗名'] + ': ' + s['種別'] + '、最低' + (s['最低必要人数/日'] || '1') + '人/日');
  });

  var rulesText = rules.length ? rules.map(function(r) { return '- ' + r; }).join('\n') : 'なし';
  var dateLabels = dates.map(function(d) {
    var dt = new Date(d + 'T00:00:00');
    return d + '(' + DOW_JP[dt.getDay()] + ')';
  });
  var staffNames = staff.map(function(s) { return s['氏名']; });
  var retailNames = retailStores.map(function(s) { return s['店舗名']; });

  // 過去シフトパターンを取得
  var pastPatterns = loadPastShiftPatterns_(staff, dates[0]);

  var prompt = '以下の条件でシフトスケジュールを生成してください。\n'
    + '各スタッフを店舗に割り当て、勤務時間を決定してください。\n\n'
    + '## 対象期間\n' + dates[0] + ' ~ ' + dates[dates.length - 1] + ' (' + dates.length + '日間)\n\n'
    + '## 日付一覧\n' + dateLabels.join(', ') + '\n\n'
    + '## スタッフ一覧 (' + staff.length + '名)\n' + staffInfo.join('\n') + '\n\n'
    + '## 店舗情報\n' + storeInfo.join('\n') + '\n\n'
    + '## 希望シフトデータ\n' + (wishLines.length ? wishLines.join('\n') : '希望データなし') + '\n\n'
    + '## 共通ルール\n' + rulesText + '\n\n'
    + '## 店舗の早番/遅番について\n'
    + '- 営業店舗（' + retailNames.join(', ') + '）は早番/遅番の2交代制\n'
    + '- 早番: 概ね9:30-16:00（開店準備-午後）\n'
    + '- 遅番: 概ね15:30-0:30（午後-閉店）\n'
    + '- 各店舗に毎日必ず早番1名/遅番1名を配置\n'
    + '- 同一人物が通し勤務する場合あり（例: 11:30-0:30）\n\n'
    + '## 労働基準法の遵守事項\n'
    + '- 1日の法定労働時間: 8時間（超過分は時間外労働=残業）\n'
    + '- 1週間の法定労働時間: 40時間\n'
    + '- 残業(時間外労働)の上限: 月45時間以内（36協定）\n'
    + '- 6時間超勤務: 45分以上の休憩が必要\n'
    + '- 8時間超勤務: 60分以上の休憩が必要\n'
    + '- 勤務間インターバル: 前日の終業から翌日の始業まで最低11時間確保（努力義務）\n'
    + '- 深夜労働(22:00-5:00): 25%割増賃金。連続しないよう配慮\n'
    + '- 週1日以上の休日を必ず確保（法定休日）\n'
    + '- 連続勤務は原則6日以内\n'
    + '- アルバイトは特に労働時間の上限に注意\n'
    + '- コスト最適化: 残業を最小限にするシフトが望ましい。1日8時間以内×週5日が理想\n'
    + '- 残業が発生する場合は特定のスタッフに偏らないよう均等に分散\n\n'
    + '## 制約\n'
    + '1. 役員を除く全スタッフ（正社員の固定シフト・アルバイトの希望シフト）を配置対象とする\n'
    + '2. 正社員（固定シフト）は週40時間（週5日）が基本。最低人数が埋まらない日がある場合のみ残業可。ただし月の残業累計は45時間以内\n'
    + '3. 希望休（×マーク）は必ず尊重する\n'
    + '4. 対応店舗が指定されているスタッフはその店舗のみに配置\n'
    + '5. 最低週1日の休みを確保\n'
    + '6. 土日祝日も含め全日全店舗カバー\n'
    + '7. 勤務間インターバル11時間を確保（遅番0:30終了→翌日11:30以降開始）\n'
    + '8. 連続勤務6日以内\n\n'
    + '## 出力形式（JSONのみ、説明文不要）\n\n'
    + '```json\n{\n'
    + '  "schedule": [\n'
    + '    {\n'
    + '      "date": "' + dates[0] + '",\n'
    + '      "stores": {\n';

  retailStores.forEach(function(s) {
    prompt += '        "' + s['店舗名'] + '": {"早番": "名前", "遅番": "名前"},\n';
  });
  otherStores.forEach(function(s) {
    prompt += '        "' + s['店舗名'] + '": ["名前"],\n';
  });

  prompt += '      },\n'
    + '      "times": {"名前1": "11:30-16:00", "名前2": "15:30-0:30"}\n'
    + '    }\n  ],\n'
    + '  "staff_groups": {"店舗名": ["スタッフ名"]},\n'
    + '  "notes": "補足"\n}\n```\n\n'
    + '### JSON出力ルール:\n'
    + '- schedule: 全' + dates.length + '日分を配列で出力\n'
    + '- stores: 営業店舗は{"早番":"名前","遅番":"名前"}、非営業は["名前"]\n'
    + '- times: その日に出勤するスタッフのみ（休みは含めない）\n'
    + '- staff_groups: 各スタッフの主要勤務店舗グルーピング。全スタッフを含めること\n'
    + '- スタッフ名は完全一致: ' + JSON.stringify(staffNames) + '\n'
    + '- 時間は "HH:MM-HH:MM" 形式\n';

  // 過去のシフトパターンを追加
  if (pastPatterns) {
    prompt += '\n## 過去の確定シフトパターン (参考)\n'
      + '以下は直近の確定済みシフトから分析したパターンです。これを参考にしてください:\n'
      + pastPatterns + '\n';
  }

  // 過去の修正履歴を追加 (学習)
  var history = loadCorrectionHistory_();
  if (history) {
    prompt += '\n## 過去の修正履歴 (人間がAI提案を修正したパターン)\n'
      + '以下は過去にAIが生成したシフトに対して人間が行った修正です。同じ傾向の修正が繰り返されないよう、このパターンを学習してください:\n'
      + history + '\n';
  }

  return prompt;
}

function callGeminiApi_(prompt) {
  var apiKey = getProp_('GEMINI_API_KEY');
  if (!apiKey) throw new Error('GEMINI_API_KEY が設定されていません');

  var model = 'gemini-2.5-flash'; // シフト専用プロジェクトのキー (クォータ独立)
  var url = 'https://generativelanguage.googleapis.com/v1beta/models/' + model + ':generateContent?key=' + apiKey;
  var payload = JSON.stringify({
    systemInstruction: {
      parts: [{ text: 'あなたはシフト管理の専門家です。与えられた条件を厳密に守り、最適なシフトスケジュールをJSON形式で生成してください。十分に検討してから、JSONのみを出力してください。' }],
    },
    contents: [{ role: 'user', parts: [{ text: prompt }] }],
    generationConfig: {
      temperature: 0,
      responseMimeType: 'application/json',
      maxOutputTokens: 65536, // 2.5-flashの最大。31日分のJSONは長い
      thinkingConfig: { thinkingBudget: 4096 }, // thinkingが出力枠を食い潰すのを防ぐ
    },
  });

  var lastErr = '';
  for (var attempt = 0; attempt < 3; attempt++) {
    var resp = UrlFetchApp.fetch(url, {
      method: 'post',
      contentType: 'application/json',
      payload: payload,
      muteHttpExceptions: true,
    });
    var code = resp.getResponseCode();
    var body = resp.getContentText();
    if (code === 200) {
      var result = JSON.parse(body);
      if (result.error) throw new Error('Gemini API: ' + JSON.stringify(result.error));
      var cand = (result.candidates || [])[0];
      if (!cand || !cand.content || !cand.content.parts) {
        throw new Error('Gemini応答が空です: ' + body.substring(0, 500));
      }
      if (cand.finishReason && cand.finishReason !== 'STOP') {
        throw new Error('Gemini応答が途中終了 (finishReason=' + cand.finishReason + ')。期間を分割して再実行してください。');
      }
      var text = '';
      cand.content.parts.forEach(function(p) { if (p.text) text += p.text; });
      return text;
    }
    lastErr = 'HTTP ' + code + ': ' + body.substring(0, 300);
    // 429 / 5xx（503高負荷など）は一時エラーとしてリトライ（3s, 6s, 9s）
    if (code === 429 || code >= 500) {
      Utilities.sleep(3000 * (attempt + 1));
      continue;
    }
    throw new Error('Gemini API: ' + lastErr);
  }
  throw new Error('Gemini API 3回リトライ失敗: ' + lastErr);
}

function parseAiResponse_(response) {
  // Gemini は responseMimeType=application/json で純JSONを返すが、
  // 稀に ```json フェンスが付く場合に備えて両対応。
  var jsonMatch = response.match(/```(?:json)?\s*\n?([\s\S]*?)\n?```/);
  var jsonStr = jsonMatch ? jsonMatch[1] : response.trim();
  try {
    return JSON.parse(jsonStr);
  } catch (e) {
    throw new Error('AI応答のJSON解析失敗: ' + e.message + '\n先頭500文字: ' + response.substring(0, 500));
  }
}

// ==========================================================================
// ③ writeShiftOutput_ (横型レイアウト対応)
// ==========================================================================

function writeShiftOutput_(result, staff, stores, dates) {
  var schedule = result.schedule || [];
  var outputSheet = sheet_(SN_OUTPUT);
  if (!outputSheet) throw new Error('シフト出力シートが見つかりません');

  // Read dynamic non-retail layout
  var nrLayout = readNonRetailLayout_(outputSheet);
  var OUTPUT_TOTAL_COLS = nrLayout.totalCols;

  // Clear data area
  outputSheet.getRange(OUTPUT_DSTART, 1, OUTPUT_DAYS * 2, 2).clearContent();
  outputSheet.getRange(OUTPUT_DSTART, 4, OUTPUT_DAYS * 2, OUTPUT_TOTAL_COLS - 3).clearContent();

  // Build schedule lookup
  var dayMap = {};
  schedule.forEach(function(dayData) { dayMap[dayData.date] = dayData; });

  // Name dropdown validation
  var activeNames = staff.map(function(s) { return s['氏名']; });
  activeNames.unshift('');
  var nameRule = SpreadsheetApp.newDataValidation().requireValueInList(activeNames, true).setAllowInvalid(true).build();

  dates.forEach(function(dateStr, idx) {
    var dayData = dayMap[dateStr] || {};
    var storeAssigns = dayData.stores || {};
    var times = dayData.times || {};
    var nameRowNum = OUTPUT_DSTART + idx * 2;
    var timeRowNum = nameRowNum + 1;

    function splitTime(name) {
      var t = times[name] || '';
      if (!t || t.indexOf('-') < 0) return ['', ''];
      var parts = t.split('-');
      return [parts[0].trim(), parts[1].trim()];
    }

    // Retail stores
    for (var storeName in RETAIL_COLS) {
      var assign = storeAssigns[storeName] || {};
      var shifts = RETAIL_COLS[storeName];

      SHIFT_TYPES.forEach(function(stype) {
        var cols = shifts[stype];
        var staffName = '';

        if (typeof assign === 'object' && !Array.isArray(assign)) {
          staffName = assign[stype] || '';
        }

        if (staffName) {
          outputSheet.getRange(nameRowNum, cols.n).setValue(staffName);
          var t = splitTime(staffName);
          if (t[0]) outputSheet.getRange(timeRowNum, cols.s).setValue(t[0]);
          if (t[1]) outputSheet.getRange(timeRowNum, cols.e).setValue(t[1]);
        }
      });
    }

    // Non-retail stores (dynamic)
    for (var storeName in nrLayout.stores) {
      var assign = storeAssigns[storeName] || [];
      if (typeof assign === 'object' && !Array.isArray(assign)) {
        assign = Object.values(assign);
      }

      var storeInfo = nrLayout.stores[storeName];
      var personCols = storeInfo.cols;
      var personNames = storeInfo.names.slice(); // copy to allow mutation

      assign.forEach(function(staffName) {
        if (!staffName) return;
        var idx = personNames.indexOf(staffName);
        if (idx < 0) {
          for (var i = 0; i < personNames.length; i++) {
            if (!personNames[i]) { idx = i; personNames[i] = staffName; break; }
          }
        }
        if (idx >= 0 && idx < personCols.length) {
          var pc = personCols[idx];
          outputSheet.getRange(nameRowNum, pc.s).setValue('出勤');
          var t = splitTime(staffName);
          if (t[0]) outputSheet.getRange(timeRowNum, pc.s).setValue(t[0]);
          if (t[1]) outputSheet.getRange(timeRowNum, pc.e).setValue(t[1]);
        }
      });
    }
  });

  Logger.log('Shift output written: ' + dates.length + ' days');
}

// ==========================================================================
// AI修正履歴 (学習機能)
// ==========================================================================

var SN_AI_LOG = 'AI修正履歴';

/**
 * AI生成結果を一時保存 (Script Properties)
 * ④実行時にシート上のデータと比較して修正を検出する
 */
function saveAiOutput_(result, dates) {
  // 日付→店舗→スタッフ配置のフラットなマップに変換
  var flat = {};
  (result.schedule || []).forEach(function(day) {
    var dateStr = day.date;
    var storeAssigns = day.stores || {};
    var times = day.times || {};

    for (var store in storeAssigns) {
      var assign = storeAssigns[store];
      var staffList = [];
      if (Array.isArray(assign)) {
        staffList = assign;
      } else if (typeof assign === 'object') {
        for (var role in assign) {
          if (assign[role]) staffList.push(assign[role]);
        }
      }
      staffList.forEach(function(name) {
        var key = dateStr + '|' + store + '|' + name;
        flat[key] = times[name] || '';
      });
    }
  });

  // Script Properties に保存 (サイズ制限あるため JSON を圧縮)
  try {
    var json = JSON.stringify(flat);
    // 9KB制限に収まらない場合は日付リストだけ保存
    if (json.length > 8000) {
      // 分割保存
      var props = PropertiesService.getScriptProperties();
      props.setProperty('AI_OUTPUT_DATES', JSON.stringify(dates));
      var chunk1 = {}, chunk2 = {};
      var keys = Object.keys(flat);
      var half = Math.ceil(keys.length / 2);
      keys.forEach(function(k, i) {
        if (i < half) chunk1[k] = flat[k];
        else chunk2[k] = flat[k];
      });
      props.setProperty('AI_OUTPUT_1', JSON.stringify(chunk1));
      props.setProperty('AI_OUTPUT_2', JSON.stringify(chunk2));
      props.deleteProperty('AI_OUTPUT');
    } else {
      var props = PropertiesService.getScriptProperties();
      props.setProperty('AI_OUTPUT', json);
      props.setProperty('AI_OUTPUT_DATES', JSON.stringify(dates));
      props.deleteProperty('AI_OUTPUT_1');
      props.deleteProperty('AI_OUTPUT_2');
    }
    Logger.log('AI output saved: ' + Object.keys(flat).length + ' assignments');
  } catch (e) {
    Logger.log('Failed to save AI output: ' + e.message);
  }
}

/**
 * 保存済みAI出力を読み込む
 */
function loadAiOutput_() {
  var props = PropertiesService.getScriptProperties();
  var json = props.getProperty('AI_OUTPUT');
  if (json) return JSON.parse(json);

  var j1 = props.getProperty('AI_OUTPUT_1');
  var j2 = props.getProperty('AI_OUTPUT_2');
  if (j1 && j2) {
    var merged = JSON.parse(j1);
    var part2 = JSON.parse(j2);
    for (var k in part2) merged[k] = part2[k];
    return merged;
  }
  return null;
}

/**
 * ④確定時にシートの内容とAI出力を比較し、修正を検出・記録する
 * syncConfirmedShift から呼ばれる
 */
function detectAndLogCorrections_(confirmedDates, outputSheet, nameToSid, nrLayout) {
  var aiOutput = loadAiOutput_();
  if (!aiOutput) return; // AI出力が保存されていない場合はスキップ

  var corrections = [];

  // シートから現在の配置を読み取り
  confirmedDates.forEach(function(dateStr) {
    var day = parseInt(dateStr.split('-')[2], 10);
    var nameRowNum = OUTPUT_DSTART + (day - 1) * 2;
    var timeRowNum = nameRowNum + 1;

    // 現在のシート上の配置を収集
    var currentAssigns = {}; // key: store|staffName, value: time

    // 営業店舗
    for (var storeName in RETAIL_COLS) {
      var shifts = RETAIL_COLS[storeName];
      SHIFT_TYPES.forEach(function(stype) {
        var cols = shifts[stype];
        var staffName = String(outputSheet.getRange(nameRowNum, cols.n).getValue()).trim();
        if (!staffName) return;
        var startTime = String(outputSheet.getRange(timeRowNum, cols.s).getValue()).trim();
        var endTime = String(outputSheet.getRange(timeRowNum, cols.e).getValue()).trim();
        var time = (startTime && endTime) ? startTime + '-' + endTime : '';
        currentAssigns[storeName + '|' + staffName] = time;
      });
    }

    // 非営業店舗
    for (var nrStoreName in nrLayout.stores) {
      var nrStoreInfo = nrLayout.stores[nrStoreName];
      for (var nri = 0; nri < nrStoreInfo.cols.length; nri++) {
        var pc = nrStoreInfo.cols[nri];
        var pName = nrStoreInfo.names[nri];
        if (!pName) continue;
        var attendance = String(outputSheet.getRange(nameRowNum, pc.s).getValue()).trim();
        if (!attendance || attendance === '公休') continue;
        var actualStore = nrStoreName;
        if (RETAIL_COLS[attendance]) actualStore = attendance;
        var startTime = String(outputSheet.getRange(timeRowNum, pc.s).getValue()).trim();
        var endTime = String(outputSheet.getRange(timeRowNum, pc.e).getValue()).trim();
        var time = (startTime && endTime) ? startTime + '-' + endTime : '';
        currentAssigns[actualStore + '|' + pName] = time;
      }
    }

    // AI出力と比較
    // AI側のこの日の配置を収集
    var aiAssigns = {};
    for (var key in aiOutput) {
      if (key.indexOf(dateStr + '|') === 0) {
        var parts = key.split('|');
        aiAssigns[parts[1] + '|' + parts[2]] = aiOutput[key];
      }
    }

    // 差分検出
    // AIにあって人間が削除した配置
    for (var aKey in aiAssigns) {
      if (!currentAssigns[aKey]) {
        var aParts = aKey.split('|');
        corrections.push({
          date: dateStr, store: aParts[0], staff: aParts[1],
          type: '削除', ai: aiAssigns[aKey], human: '',
        });
      }
    }
    // 人間が追加した配置
    for (var cKey in currentAssigns) {
      if (!aiAssigns[cKey]) {
        var cParts = cKey.split('|');
        corrections.push({
          date: dateStr, store: cParts[0], staff: cParts[1],
          type: '追加', ai: '', human: currentAssigns[cKey],
        });
      }
    }
    // 時間が変更された配置
    for (var key in currentAssigns) {
      if (aiAssigns[key] && aiAssigns[key] !== currentAssigns[key]) {
        var kParts = key.split('|');
        corrections.push({
          date: dateStr, store: kParts[0], staff: kParts[1],
          type: '時間変更', ai: aiAssigns[key], human: currentAssigns[key],
        });
      }
    }
  });

  if (corrections.length === 0) return;

  // 修正履歴シートに記録
  var logSheet = ss_().getSheetByName(SN_AI_LOG);
  if (!logSheet) {
    logSheet = ss_().insertSheet(SN_AI_LOG);
    logSheet.getRange(1, 1, 1, 7).setValues([['記録日時', '日付', '店舗', 'スタッフ', '修正種別', 'AI提案', '人間修正']]);
    logSheet.getRange(1, 1, 1, 7).setFontWeight('bold').setBackground('#E8EAED');
  }

  var now = Utilities.formatDate(new Date(), Session.getScriptTimeZone(), 'yyyy-MM-dd HH:mm');
  var rows = corrections.map(function(c) {
    return [now, c.date, c.store, c.staff, c.type, c.ai, c.human];
  });

  var lastRow = Math.max(logSheet.getLastRow(), 1);
  logSheet.getRange(lastRow + 1, 1, rows.length, 7).setValues(rows);

  Logger.log('Recorded ' + corrections.length + ' corrections to AI修正履歴');
}

/**
 * 過去の修正履歴をプロンプト用テキストとして読み込む
 * 直近50件を取得
 */
function loadCorrectionHistory_() {
  var logSheet = ss_().getSheetByName(SN_AI_LOG);
  if (!logSheet) return '';

  var lastRow = logSheet.getLastRow();
  if (lastRow <= 1) return '';

  // 直近50件
  var startRow = Math.max(2, lastRow - 49);
  var numRows = lastRow - startRow + 1;
  var data = logSheet.getRange(startRow, 1, numRows, 7).getValues();

  // パターン集約: 同じスタッフ×店舗×修正種別をまとめる
  var patterns = {};
  data.forEach(function(row) {
    var staff = row[3];
    var store = row[2];
    var type = row[4];
    var key = staff + '→' + store + '(' + type + ')';
    if (!patterns[key]) patterns[key] = { count: 0, examples: [] };
    patterns[key].count++;
    if (patterns[key].examples.length < 2) {
      patterns[key].examples.push({
        date: row[1], ai: row[5], human: row[6],
      });
    }
  });

  var lines = [];
  for (var key in patterns) {
    var p = patterns[key];
    var line = key + ' (' + p.count + '回)';
    p.examples.forEach(function(ex) {
      if (ex.ai && ex.human) {
        line += ' 例: AI=' + ex.ai + '→人間=' + ex.human;
      } else if (!ex.ai) {
        line += ' 例: ' + ex.date + 'に人間が追加';
      } else {
        line += ' 例: ' + ex.date + 'にAI提案を削除';
      }
    });
    lines.push('- ' + line);
  }

  return lines.join('\n');
}

/**
 * kintone 212から直近の確定シフトを読み取り、パターンを分析する
 * @param {Array} staff スタッフ一覧
 * @param {string} targetStartDate 生成対象の開始日 (これより前のデータを参照)
 * @return {string} パターン説明テキスト (プロンプト用)
 */
function loadPastShiftPatterns_(staff, targetStartDate) {
  // 対象期間: 生成対象の前月分 (最大31日)
  var target = new Date(targetStartDate + 'T00:00:00');
  var prevEnd = new Date(target);
  prevEnd.setDate(prevEnd.getDate() - 1);
  var prevStart = new Date(prevEnd);
  prevStart.setDate(prevStart.getDate() - 30);

  var startStr = Utilities.formatDate(prevStart, Session.getScriptTimeZone(), 'yyyy-MM-dd');
  var endStr = Utilities.formatDate(prevEnd, Session.getScriptTimeZone(), 'yyyy-MM-dd');

  var records = readShiftDataRange_(startStr, endStr).filter(function(r) {
    return r.shift_status === '出勤';
  });

  if (records.length < 10) return ''; // データ不足

  // スタッフごとの統計
  var staffStats = {}; // { name: { stores: {store: count}, totalDays: N, avgHours: N, weekdayPattern: {} } }
  var nameToSid = {};
  staff.forEach(function(s) { nameToSid[s['氏名']] = s['staff_id']; });

  records.forEach(function(r) {
    var name = r.staff_name;
    var store = r.store;
    var start = r.start_time;
    var end = r.end_time;
    var dateStr = r.date;

    if (!name || !store) return;
    if (!staffStats[name]) staffStats[name] = { stores: {}, totalDays: 0, totalHours: 0, dowCount: [0,0,0,0,0,0,0] };
    var s = staffStats[name];

    if (!s.stores[store]) s.stores[store] = 0;
    s.stores[store]++;
    s.totalDays++;

    // 勤務時間計算
    if (start && end) {
      var sp = start.split(':'), ep = end.split(':');
      var sm = parseInt(sp[0]) * 60 + parseInt(sp[1] || 0);
      var em = parseInt(ep[0]) * 60 + parseInt(ep[1] || 0);
      if (em <= sm) em += 24 * 60;
      s.totalHours += (em - sm) / 60;
    }

    // 曜日パターン
    if (dateStr) {
      var d = new Date(dateStr + 'T00:00:00');
      s.dowCount[d.getDay()]++;
    }
  });

  // パターンテキスト生成
  var lines = [];
  var dowNames = ['日', '月', '火', '水', '木', '金', '土'];

  for (var name in staffStats) {
    var s = staffStats[name];
    if (s.totalDays < 3) continue; // データ少なすぎ

    var avgHours = Math.round(s.totalHours / s.totalDays * 10) / 10;

    // 主要店舗 (出勤回数順)
    var storeEntries = Object.keys(s.stores).map(function(st) {
      return { name: st, count: s.stores[st] };
    }).sort(function(a, b) { return b.count - a.count; });
    var storeText = storeEntries.map(function(e) { return e.name + '(' + e.count + '日)'; }).join(', ');

    // 出勤が多い曜日
    var workDows = [];
    var restDows = [];
    var maxDow = Math.max.apply(null, s.dowCount);
    for (var di = 0; di < 7; di++) {
      if (s.dowCount[di] === 0) restDows.push(dowNames[di]);
      else if (s.dowCount[di] >= maxDow * 0.7) workDows.push(dowNames[di]);
    }

    var line = name + ': ' + storeText + ', 平均' + avgHours + 'h/日, ' + s.totalDays + '日出勤';
    if (restDows.length > 0 && restDows.length <= 3) line += ', 休み傾向: ' + restDows.join('');
    lines.push('- ' + line);
  }

  if (lines.length === 0) return '';
  return lines.join('\n');
}

// ==========================================================================
// ④ 確定シフト反映 (横型レイアウト対応)
// ==========================================================================

/**
 * シートの1日分のデータを読み取る
 */
function readDayEntries_(outputSheet, nameRowNum, timeRowNum, dateStr, nameToSid, nrLayout) {
  var entries = [];

  // Read retail stores
  for (var storeName in RETAIL_COLS) {
    var shifts = RETAIL_COLS[storeName];
    SHIFT_TYPES.forEach(function(stype) {
      var cols = shifts[stype];
      var staffName = String(outputSheet.getRange(nameRowNum, cols.n).getValue()).trim();
      if (!staffName) return;

      var startTime = String(outputSheet.getRange(timeRowNum, cols.s).getValue()).trim();
      var endTime = String(outputSheet.getRange(timeRowNum, cols.e).getValue()).trim();

      entries.push({
        date: dateStr,
        staff_id: nameToSid[staffName] || '',
        staff_name: staffName,
        store: storeName,
        start_time: startTime,
        end_time: endTime,
        time_range: startTime && endTime ? startTime + '-' + endTime : '',
        shift_type: stype,
        shift_status: '出勤',
      });
    });
  }

  // Read non-retail stores (dynamic)
  for (var nrStoreName in nrLayout.stores) {
    var nrStoreInfo = nrLayout.stores[nrStoreName];
    for (var nri = 0; nri < nrStoreInfo.cols.length; nri++) {
      var pc = nrStoreInfo.cols[nri];
      var pName = nrStoreInfo.names[nri];
      if (!pName) continue;

      var attendance = String(outputSheet.getRange(nameRowNum, pc.s).getValue()).trim();
      if (!attendance) continue;

      var actualStore = nrStoreName;
      var shiftStatus = '出勤';
      if (attendance === '公休') {
        shiftStatus = '公休';
      } else if (RETAIL_COLS[attendance]) {
        actualStore = attendance;
      }

      var startTime = '';
      var endTime = '';
      if (shiftStatus === '出勤') {
        startTime = String(outputSheet.getRange(timeRowNum, pc.s).getValue()).trim();
        endTime = String(outputSheet.getRange(timeRowNum, pc.e).getValue()).trim();
      }

      entries.push({
        date: dateStr,
        staff_id: nameToSid[pName] || '',
        staff_name: pName,
        store: actualStore,
        start_time: startTime,
        end_time: endTime,
        time_range: startTime && endTime ? startTime + '-' + endTime : '',
        shift_type: '',
        shift_status: shiftStatus,
      });
    }
  }

  return entries;
}

// ==========================================================================
// v2: 確定シフトの正本 = 「シフトデータ」タブ (kintone 212 の置き換え)
// ==========================================================================

var SN_SHIFT_DATA = 'シフトデータ';
var SHIFT_DATA_HEADERS = ['shift_date', 'staff_id', 'staff_name', 'shift_status', 'store', 'start_time', 'end_time', 'period_start', 'period_end', 'confirmed_by', 'confirmed_at'];

function shiftDataSheet_() {
  var ss = SpreadsheetApp.openById(SS_ID);
  var sh = ss.getSheetByName(SN_SHIFT_DATA);
  if (!sh) {
    sh = ss.insertSheet(SN_SHIFT_DATA);
    sh.getRange('A:K').setNumberFormat('@'); // 全列テキスト形式 (日付/時刻の自動変換を防ぐ)
    sh.getRange(1, 1, 1, SHIFT_DATA_HEADERS.length).setValues([SHIFT_DATA_HEADERS]).setFontWeight('bold');
    sh.setFrozenRows(1);
    try { sh.hideSheet(); } catch (e) {} // 正本タブは非表示運用
  }
  return sh;
}

// Date/文字列どちらでも 'yyyy-MM-dd' に正規化
function normDateStr_(v) {
  if (v instanceof Date) return Utilities.formatDate(v, Session.getScriptTimeZone(), 'yyyy-MM-dd');
  var s = String(v || '').trim();
  var m = s.match(/^(\d{4})[\/\-](\d{1,2})[\/\-](\d{1,2})/);
  if (!m) return '';
  return m[1] + '-' + ('0' + m[2]).slice(-2) + '-' + ('0' + m[3]).slice(-2);
}

// Date/文字列どちらでも 'H:mm' に正規化
function normTimeStr_(v) {
  if (v instanceof Date) return Utilities.formatDate(v, Session.getScriptTimeZone(), 'H:mm');
  return String(v || '').trim();
}

// 期間内の確定シフトを読み込み (kintoneGetAll_ 相当)
function readShiftDataRange_(startDate, endDate) {
  var sh = shiftDataSheet_();
  var last = sh.getLastRow();
  if (last < 2) return [];
  var vals = sh.getRange(2, 1, last - 1, SHIFT_DATA_HEADERS.length).getValues();
  var out = [];
  vals.forEach(function(r, i) {
    var date = normDateStr_(r[0]);
    if (!date || date < startDate || date > endDate) return;
    out.push({
      date: date,
      staff_id: String(r[1] || ''),
      staff_name: String(r[2] || ''),
      shift_status: String(r[3] || '出勤'),
      store: String(r[4] || ''),
      start_time: normTimeStr_(r[5]),
      end_time: normTimeStr_(r[6]),
      _row: i + 2,
    });
  });
  return out;
}

// 指定日付のレコードを全削除 (再確定=delete→append の冪等パターン)
function deleteShiftDataRows_(dates) {
  if (!dates.length) return 0;
  var set = {};
  dates.forEach(function(d) { set[d] = true; });
  var sh = shiftDataSheet_();
  var last = sh.getLastRow();
  if (last < 2) return 0;
  var vals = sh.getRange(2, 1, last - 1, 1).getValues();
  var rows = [];
  vals.forEach(function(r, i) { if (set[normDateStr_(r[0])]) rows.push(i + 2); });
  for (var i = rows.length - 1; i >= 0; i--) sh.deleteRow(rows[i]); // 下から削除で行ズレ防止
  return rows.length;
}

function appendShiftData_(entries, periodStart, periodEnd, approver, approvedAt) {
  if (!entries.length) return 0;
  var sh = shiftDataSheet_();
  var rows = entries.map(function(d) {
    return [d.date, d.staff_id, d.staff_name, d.shift_status || '出勤', d.store || '',
            d.start_time || '', d.end_time || '', periodStart, periodEnd, approver, approvedAt];
  });
  sh.getRange(sh.getLastRow() + 1, 1, rows.length, rows[0].length).setValues(rows);
  return rows.length;
}

function syncConfirmedShift() {
  ensureTrigger_();
  var ui = SpreadsheetApp.getUi();
  try {
    var outputSheet = sheet_(SN_OUTPUT);
    if (!outputSheet) { ui.alert('シフト出力シートが見つかりません'); return; }

    var ym = getSelectedYearMonth_();
    if (!ym) { ui.alert('年月セレクター(シフト出力 A1=年/A2=月)を設定してください'); return; }
    var year = ym.year;
    var month = ym.month;
    var m = ('0' + month).slice(-2);

    var approver = Session.getActiveUser().getEmail();
    var approvedAt = Utilities.formatDate(new Date(), Session.getScriptTimeZone(), 'yyyy-MM-dd HH:mm:ss');

    var staff = readStaffMaster_();
    var nameToSid = {};
    staff.forEach(function(s) { nameToSid[s['氏名']] = s['staff_id']; });

    var nrLayout = readNonRetailLayout_(outputSheet);
    var lastDay = new Date(parseInt(year), parseInt(month), 0).getDate();

    // --- シフトデータ(正本タブ)の既存レコードを取得 ---
    var monthStart = year + '-' + m + '-01';
    var monthEnd = year + '-' + m + '-' + ('0' + lastDay).slice(-2);
    var existingRecords = readShiftDataRange_(monthStart, monthEnd);

    // 日付ごとの既存有無を集める
    var existingByDate = {};
    existingRecords.forEach(function(r) {
      if (!existingByDate[r.date]) existingByDate[r.date] = [];
      existingByDate[r.date].push(r);
    });

    // --- シートをスキャンして3カテゴリに分類 ---
    var newEntries = [];     // 確定ON & 未登録 → 新規追加
    var updateEntries = [];  // 確定ON & 変更ON → 更新
    var deleteDates = [];    // 確定OFF & 登録済 → 削除
    var changeRowNums = [];  // 変更チェックを外す行

    for (var day = 1; day <= lastDay; day++) {
      var nameRowNum = OUTPUT_DSTART + (day - 1) * 2;
      var timeRowNum = nameRowNum + 1;
      var dateStr = year + '-' + m + '-' + ('0' + day).slice(-2);

      var isConfirmed = outputSheet.getRange(nameRowNum, 1).getValue() === true;
      var isChanged = outputSheet.getRange(nameRowNum, 2).getValue() === true;
      var existsInKintone = !!existingByDate[dateStr];

      if (isConfirmed && isChanged) {
        // 変更あり → 既存データを更新
        var entries = readDayEntries_(outputSheet, nameRowNum, timeRowNum, dateStr, nameToSid, nrLayout);
        updateEntries = updateEntries.concat(entries);
        changeRowNums.push(nameRowNum);
      } else if (isConfirmed && !isChanged) {
        if (!existsInKintone) {
          // 新規確定
          var entries = readDayEntries_(outputSheet, nameRowNum, timeRowNum, dateStr, nameToSid, nrLayout);
          newEntries = newEntries.concat(entries);
        }
        // 既にkintoneにある → スキップ
      } else if (!isConfirmed && existsInKintone) {
        // 確定チェックOFF & kintoneに存在 → 削除
        deleteDates.push(dateStr);
      }
    }

    if (!newEntries.length && !updateEntries.length && !deleteDates.length) {
      ui.alert('処理対象がありません。\n\n新規確定・変更・削除いずれも検出されませんでした。');
      return;
    }

    // --- 確認ダイアログ ---
    var msgParts = [];
    if (newEntries.length) msgParts.push('新規確定: ' + newEntries.length + '件');
    if (updateEntries.length) msgParts.push('変更更新: ' + updateEntries.length + '件');
    if (deleteDates.length) msgParts.push('削除: ' + deleteDates.length + '日分');
    var confirmMsg = msgParts.join('\n') + '\n\nシフトデータとGoogleカレンダーに反映しますか？';
    if (ui.alert('確定シフト反映', confirmMsg, ui.ButtonSet.YES_NO) !== ui.Button.YES) return;

    var dataAdded = 0, dataDeleted = 0;
    var calCreated = 0, calDeleted = 0;

    // --- 1) 新規確定 → シフトデータ追加 + カレンダー作成 ---
    if (newEntries.length) {
      var allDates = newEntries.map(function(d) { return d.date; });
      var periodStart = allDates.sort()[0];
      var periodEnd = allDates[allDates.length - 1];
      dataAdded += appendShiftData_(newEntries, periodStart, periodEnd, approver, approvedAt);
      var cr = syncToCalendar_(newEntries, year, false);
      calCreated += cr.created;
    }

    // --- 2) 変更更新 → 対象日を全削除→再追加 (冪等) + カレンダー削除→再作成 ---
    if (updateEntries.length) {
      var updateDateSet = {};
      updateEntries.forEach(function(d) { updateDateSet[d.date] = true; });
      var updateDateList = Object.keys(updateDateSet).sort();

      dataDeleted += deleteShiftDataRows_(updateDateList);
      var periodStart = updateDateList[0];
      var periodEnd = updateDateList[updateDateList.length - 1];
      dataAdded += appendShiftData_(updateEntries, periodStart, periodEnd, approver, approvedAt);

      // カレンダー: 削除→再作成
      var cr = syncToCalendar_(updateEntries, year, true);
      calDeleted += cr.deleted;
      calCreated += cr.created;
    }

    // --- 3) 削除 → シフトデータ削除 + カレンダーイベント削除 ---
    if (deleteDates.length) {
      dataDeleted += deleteShiftDataRows_(deleteDates);
      var dr = deleteCalendarEvents_(deleteDates);
      calDeleted += dr.deleted;
    }

    // --- 変更チェックだけ外す (確定チェックは触らない) ---
    changeRowNums.forEach(function(rowNum) { outputSheet.getRange(rowNum, 2).setValue(false); });

    // --- AI修正履歴: 確定データとAI出力を比較して修正を記録 ---
    var allConfirmedDates = [];
    newEntries.concat(updateEntries).forEach(function(e) {
      if (allConfirmedDates.indexOf(e.date) < 0) allConfirmedDates.push(e.date);
    });
    if (allConfirmedDates.length > 0) {
      try {
        detectAndLogCorrections_(allConfirmedDates, outputSheet, nameToSid, nrLayout);
      } catch (logErr) {
        Logger.log('Correction logging error: ' + logErr.message);
      }
    }

    ui.alert(
      '確定シフト反映完了\n\n' +
      'シフトデータ: ' + dataAdded + '件登録 / ' + dataDeleted + '件削除\n' +
      'カレンダー: ' + calCreated + '件作成 / ' + calDeleted + '件削除'
    );

    var slackParts = ['*確定シフト反映完了*'];
    if (newEntries.length || updateEntries.length) slackParts.push('登録: ' + dataAdded + '件');
    if (deleteDates.length) slackParts.push('削除: ' + deleteDates.length + '日分');
    slackPost_(slackParts.join('\n'));
  } catch (e) {
    slackError_('syncConfirmedShift', e.message);
    ui.alert('エラー: ' + e.message);
  }
}


// Store title and color mapping (shared by calendar functions)
var STORE_CAL_CONFIG = {
  '藤沢': { title: '藤沢店', colorId: '9' },
  '伊勢佐木町': { title: '伊勢佐木町店', colorId: '10' },
  '新宿': { title: '新宿店', colorId: '6' },
  '工場': { title: '工場', colorId: '5' },
  '本部オフィス': { title: '本部オフィス', colorId: '8' },
  'EC': { title: 'EC', colorId: '3' },
};

/**
 * カレンダーにイベントを作成 (deleteOld=trueの場合は既存を先に削除)
 */
function syncToCalendar_(confirmedDays, year, deleteOld) {
  var calId = getProp_('SHIFT_CALENDAR_ID');
  if (!calId) throw new Error('SHIFT_CALENDAR_IDが設定されていません');

  var staffMaster = readStaffMaster_();
  var nameToEmail = {};
  staffMaster.forEach(function(s) {
    if (s['メールアドレス']) nameToEmail[s['氏名']] = s['メールアドレス'];
  });

  // Group by date + store
  var byDateStore = {};
  confirmedDays.forEach(function(d) {
    if (d.shift_status === '公休') return;
    var key = d.date + '_' + d.store;
    if (!byDateStore[key]) byDateStore[key] = { date: d.date, store: d.store, staff: [] };
    byDateStore[key].staff.push(d);
  });

  var deleted = 0, created = 0;
  var cal = CalendarApp.getCalendarById(calId);
  var token = ScriptApp.getOAuthToken();

  for (var key in byDateStore) {
    var entry = byDateStore[key];
    var config = STORE_CAL_CONFIG[entry.store];
    if (!config) continue;

    // --- 既存イベント削除 (変更更新時のみ、[shift-sync]タグ付きのみ対象) ---
    if (deleteOld) {
      var d = new Date(entry.date + 'T00:00:00');
      var nextD = new Date(d);
      nextD.setDate(nextD.getDate() + 1);
      try {
        var existingEvents = cal.getEvents(d, nextD, { search: config.title });
        existingEvents.forEach(function(ev) {
          if (ev.getTitle() === config.title && (ev.getDescription() || '').indexOf('[shift-sync]') >= 0) {
            ev.deleteEvent();
            deleted++;
          }
        });
      } catch (e) {
        Logger.log('Calendar delete error: ' + e.message);
      }
    }

    // --- イベント作成 ---
    var descLines = [];
    entry.staff.sort(function(a, b) { return (a.start_time || '99') < (b.start_time || '99') ? -1 : 1; });
    entry.staff.forEach(function(s) {
      if (s.start_time && s.end_time) {
        descLines.push(s.start_time + '-' + s.end_time + ' ' + s.staff_name);
      } else {
        descLines.push(s.staff_name);
      }
    });

    var attendees = [];
    entry.staff.forEach(function(s) {
      var email = nameToEmail[s.staff_name];
      if (email) attendees.push({ email: email });
    });

    var nextDay = new Date(new Date(entry.date + 'T00:00:00'));
    nextDay.setDate(nextDay.getDate() + 1);
    var nextDayStr = Utilities.formatDate(nextDay, Session.getScriptTimeZone(), 'yyyy-MM-dd');

    var event = {
      summary: config.title,
      description: descLines.join('\n') + '\n\n[shift-sync]',
      start: { date: entry.date },
      end: { date: nextDayStr },
      colorId: config.colorId,
      attendees: attendees,
    };

    try {
      var resp = UrlFetchApp.fetch(
        'https://www.googleapis.com/calendar/v3/calendars/' + encodeURIComponent(calId) + '/events?sendUpdates=none',
        {
          method: 'post',
          contentType: 'application/json',
          headers: { 'Authorization': 'Bearer ' + token },
          payload: JSON.stringify(event),
          muteHttpExceptions: true,
        }
      );
      var code = resp.getResponseCode();
      if (code >= 200 && code < 300) {
        created++;
      } else {
        Logger.log('Calendar create failed (' + code + '): ' + resp.getContentText());
      }
    } catch (e) {
      Logger.log('Calendar create error: ' + e.message);
    }
  }

  return { created: created, deleted: deleted };
}

/**
 * 指定日付のカレンダーイベントを全店舗分削除
 */
function deleteCalendarEvents_(dates) {
  var calId = getProp_('SHIFT_CALENDAR_ID');
  if (!calId) return { deleted: 0 };

  var cal = CalendarApp.getCalendarById(calId);
  var deleted = 0;

  dates.forEach(function(dateStr) {
    var d = new Date(dateStr + 'T00:00:00');
    var nextD = new Date(d);
    nextD.setDate(nextD.getDate() + 1);

    for (var storeName in STORE_CAL_CONFIG) {
      var title = STORE_CAL_CONFIG[storeName].title;
      try {
        var events = cal.getEvents(d, nextD, { search: title });
        events.forEach(function(ev) {
          if (ev.getTitle() === title && (ev.getDescription() || '').indexOf('[shift-sync]') >= 0) {
            ev.deleteEvent();
            deleted++;
          }
        });
      } catch (e) {
        Logger.log('Calendar delete error: ' + e.message);
      }
    }
  });

  return { deleted: deleted };
}

// ==========================================================================
// ==========================================================================
// v2 新機能ブロック
//   1) 希望データ (正本タブ・kintone 211 の置き換え)
//   2) Slack Web App (doPost・Cloud Run Bolt の置き換え)
//   3) 勤怠 (出勤確認・遅刻記録)
//   4) 個人シフト (年月×名前セレクターの個人ビュー)
// ==========================================================================
// ==========================================================================

var SN_WISH_DATA = '希望データ';
var SN_KINTAI = '勤怠';
var SN_PERSONAL = '個人シフト';
var WISH_DATA_HEADERS = ['date', 'staff_id', 'staff_name', 'wish_type', 'start_time', 'end_time', 'channel', 'updated_at'];
var KINTAI_HEADERS = ['date', 'staff_id', 'staff_name', 'store', 'start_time', 'notified_at', 'renotified_at', 'response', 'late_minutes', 'responded_at', 'alerted'];

// --------------------------------------------------------------------------
// 1) 希望データ (正本タブ)
// --------------------------------------------------------------------------

function wishDataSheet_() {
  var ss = SpreadsheetApp.openById(SS_ID);
  var sh = ss.getSheetByName(SN_WISH_DATA);
  if (!sh) {
    sh = ss.insertSheet(SN_WISH_DATA);
    sh.getRange('A:H').setNumberFormat('@');
    sh.getRange(1, 1, 1, WISH_DATA_HEADERS.length).setValues([WISH_DATA_HEADERS]).setFontWeight('bold');
    sh.setFrozenRows(1);
    try { sh.hideSheet(); } catch (e) {} // 正本タブは非表示運用
  }
  return sh;
}

function readWishDataRange_(startDate, endDate) {
  var sh = wishDataSheet_();
  var last = sh.getLastRow();
  if (last < 2) return [];
  var vals = sh.getRange(2, 1, last - 1, WISH_DATA_HEADERS.length).getValues();
  var out = [];
  vals.forEach(function(r, i) {
    var date = normDateStr_(r[0]);
    if (!date || date < startDate || date > endDate) return;
    out.push({
      date: date,
      staff_id: String(r[1] || ''),
      staff_name: String(r[2] || ''),
      wish_type: String(r[3] || ''),
      start_time: normTimeStr_(r[4]),
      end_time: normTimeStr_(r[5]),
      channel: String(r[6] || ''),
      _row: i + 2,
    });
  });
  return out;
}

// (date, staff_id) をキーに upsert
function upsertWishData_(dateStr, staffId, staffName, wishType, startTime, endTime, channel) {
  var lock = LockService.getScriptLock();
  lock.waitLock(20000);
  try {
    var sh = wishDataSheet_();
    var now = Utilities.formatDate(new Date(), Session.getScriptTimeZone(), 'yyyy-MM-dd HH:mm:ss');
    var rowVals = [dateStr, staffId, staffName, wishType, startTime || '', endTime || '', channel || '', now];
    var last = sh.getLastRow();
    if (last >= 2) {
      var vals = sh.getRange(2, 1, last - 1, 2).getValues();
      for (var i = 0; i < vals.length; i++) {
        if (normDateStr_(vals[i][0]) === dateStr && String(vals[i][1]) === staffId) {
          sh.getRange(i + 2, 1, 1, rowVals.length).setValues([rowVals]);
          return 'updated';
        }
      }
    }
    sh.getRange(last + 1, 1, 1, rowVals.length).setValues([rowVals]);
    return 'created';
  } finally {
    lock.releaseLock();
  }
}

function removeWishData_(dateStr, staffId) {
  var lock = LockService.getScriptLock();
  lock.waitLock(20000);
  try {
    var sh = wishDataSheet_();
    var last = sh.getLastRow();
    if (last < 2) return false;
    var vals = sh.getRange(2, 1, last - 1, 2).getValues();
    for (var i = vals.length - 1; i >= 0; i--) {
      if (normDateStr_(vals[i][0]) === dateStr && String(vals[i][1]) === staffId) {
        sh.deleteRow(i + 2);
        return true;
      }
    }
    return false;
  } finally {
    lock.releaseLock();
  }
}

// 希望収集データ(グリッド)の手編集 → 希望データへ同期 (onEditTrigger から呼ばれる)
function syncWishCellToData_(sheet, row, col, sub) {
  // 対象スタッフ名: ペアの開始列(出勤列) or 希望休列 の Row2
  var startCol = (sub === '退勤') ? col - 1 : col;
  var staffName = String(sheet.getRange(2, startCol).getValue()).trim();
  if (!staffName) return;

  // 日付: A列 (例 '7/1' or Date) + D1の年
  var dateCell = sheet.getRange(row, 1).getValue();
  var year = String(sheet.getRange(1, 1).getValue()).trim();
  var dateStr = '';
  if (dateCell instanceof Date) {
    dateStr = Utilities.formatDate(dateCell, Session.getScriptTimeZone(), 'yyyy-MM-dd');
  } else {
    var md = String(dateCell).trim().match(/^(\d{1,2})\/(\d{1,2})$/);
    if (!md || !year) return;
    dateStr = year + '-' + ('0' + md[1]).slice(-2) + '-' + ('0' + md[2]).slice(-2);
  }

  var staff = readStaffMaster_();
  var staffId = '';
  staff.forEach(function(s) { if (s['氏名'] === staffName) staffId = s['staff_id']; });
  if (!staffId) return;

  if (sub === '希望休') {
    var v = String(sheet.getRange(row, startCol).getValue()).trim();
    if (v === '希望休') upsertWishData_(dateStr, staffId, staffName, '休み希望', '', '', 'sheet');
    else removeWishData_(dateStr, staffId);
    return;
  }

  // 出勤/退勤ペア
  var sv = String(sheet.getRange(row, startCol).getValue()).trim();
  var ev = String(sheet.getRange(row, startCol + 1).getValue()).trim();
  var isX = function(x) { return x === '×' || x === 'x' || x === '✕'; };
  if (isX(sv) || isX(ev)) {
    upsertWishData_(dateStr, staffId, staffName, '休み希望', '', '', 'sheet');
  } else if (sv && ev) {
    upsertWishData_(dateStr, staffId, staffName, '出勤', sv, ev, 'sheet');
  } else if (!sv && !ev) {
    removeWishData_(dateStr, staffId);
  }
  // 片方だけ入力の途中状態は同期しない (ペア完成を待つ)
}

// Slack からの希望入力 → 表示中の月なら希望収集グリッドにも即反映
function updateWishGridCell_(dateStr, staffName, wishType, startTime, endTime) {
  var wishSheet = sheet_(SN_WISH);
  if (!wishSheet) return;
  var y = String(wishSheet.getRange(1, 1).getValue()).trim();
  var mo = String(wishSheet.getRange(2, 1).getValue()).trim();
  var parts = dateStr.split('-');
  if (y !== parts[0] || String(parseInt(mo, 10)) !== String(parseInt(parts[1], 10))) return; // 表示月でない
  var day = parseInt(parts[2], 10);
  var row = 3 + day; // Row4 = 1日

  var lastCol = wishSheet.getLastColumn();
  var nameRow = wishSheet.getRange(2, 1, 1, lastCol).getValues()[0];
  var subRow = wishSheet.getRange(3, 1, 1, lastCol).getValues()[0];
  for (var c = 2; c < nameRow.length; c++) {
    if (String(nameRow[c]).trim() !== staffName) continue;
    var sub = String(subRow[c]).trim();
    if (sub === '出勤') {
      if (wishType === '休み希望' || wishType === '希望休') {
        wishSheet.getRange(row, c + 1, 1, 2).setValues([['×', '×']]).setBackground('#9E9E9E');
      } else {
        wishSheet.getRange(row, c + 1, 1, 2).setValues([[startTime || '', endTime || '']]).setBackground(null);
      }
      return;
    }
    if (sub === '希望休') {
      if (wishType === '休み希望' || wishType === '希望休') {
        wishSheet.getRange(row, c + 1).setValue('希望休');
        wishSheet.getRange(row, c + 1).setBackground('#9E9E9E');
      } else {
        wishSheet.getRange(row, c + 1).setValue('');
        wishSheet.getRange(row, c + 1).setBackground(null);
      }
      return;
    }
  }
}

// --------------------------------------------------------------------------
// 2) Slack Web App (doPost) — Cloud Run Bolt の置き換え
//    リクエストURL: https://script.google.com/macros/s/<DEP_ID>/exec?token=<WEBHOOK_TOKEN>
//    GASはSlack署名検証不可のため ?token= で簡易ガード (p10と同方式)
// --------------------------------------------------------------------------

function doPost(e) {
  try {
    var token = (e && e.parameter && e.parameter.token) || '';
    if (!token || token !== getProp_('WEBHOOK_TOKEN')) {
      return ContentService.createTextOutput('forbidden');
    }
    if (e.parameter && e.parameter.probe) {
      return ContentService.createTextOutput('shift-v2.1');
    }
    // 管理用アクション (トークン保護下・保守/検証用)
    if (e.parameter && e.parameter.admin) {
      var act = e.parameter.admin;
      if (act === 'reload') {
        var y = e.parameter.year, mo = e.parameter.month;
        var msg1 = loadShiftOutputData_(y, mo);
        var msg2 = '';
        try { msg2 = loadWishSheetData_(y, mo); } catch (e2) { msg2 = 'wish: ' + e2.message; }
        return ContentService.createTextOutput('reload ok: ' + msg1 + ' / ' + msg2);
      }
      if (act === 'personal') {
        ensurePersonalSheet_();
        renderPersonalShift_();
        return ContentService.createTextOutput('personal ok');
      }
      if (act === 'setprop') {
        var allowed = { 'GEMINI_API_KEY': 1, 'ATTEND_NOTIFY_MIN': 1, 'ATTEND_RENOTIFY_MIN': 1 };
        var k = e.parameter.propkey || '';
        if (!allowed[k]) return ContentService.createTextOutput('prop not allowed');
        PropertiesService.getScriptProperties().setProperty(k, e.parameter.propvalue || '');
        return ContentService.createTextOutput('setprop ok: ' + k);
      }
      if (act === 'staffsync') {
        var staff = readStaffMaster_();
        var c = updateWishSheetHeaders_(staff);
        var r = updateShiftOutputLayout_(staff);
        return ContentService.createTextOutput('staffsync ok: wish=' + c + ' output=' + (r && r.message));
      }
      if (act === 'calsync') {
        // 検証用: テストエントリ1件をカレンダー同期 (掃除は admin=caldel)
        var cd = e.parameter.date || '';
        if (!/^\d{4}-\d{2}-\d{2}$/.test(cd)) return ContentService.createTextOutput('date required (YYYY-MM-DD)');
        if (e.parameter.debug === '1') {
          // Calendar API 直POSTの生レスポンスを返す (失敗原因の特定用)
          var dbgResp = UrlFetchApp.fetch(
            'https://www.googleapis.com/calendar/v3/calendars/' + encodeURIComponent(getProp_('SHIFT_CALENDAR_ID')) + '/events?sendUpdates=none',
            { method: 'post', contentType: 'application/json',
              headers: { 'Authorization': 'Bearer ' + ScriptApp.getOAuthToken() },
              payload: JSON.stringify({ summary: '藤沢店', description: '[shift-sync]', start: { date: cd }, end: { date: cd }, colorId: '9' }),
              muteHttpExceptions: true });
          return ContentService.createTextOutput('debug ' + dbgResp.getResponseCode() + ': ' + dbgResp.getContentText().slice(0, 500));
        }
        var cres = syncToCalendar_([{
          date: cd,
          store: e.parameter.store || '藤沢',
          staff_name: e.parameter.name || '【検証】テスト',
          start_time: e.parameter.start || '11:00',
          end_time: e.parameter.end || '20:00',
          shift_status: '出勤',
        }], cd.slice(0, 4), e.parameter.deleteold === '1');
        return ContentService.createTextOutput('calsync ok: created=' + cres.created + ' deleted=' + cres.deleted);
      }
      if (act === 'triggers') {
        // 復旧用: 3トリガー (onEdit/勤怠cron/希望リマインド) を未設定なら作成
        var t1 = ensureTrigger_();
        var t2 = ensureAttendanceTrigger_();
        var t3 = false;
        var hasDaily = ScriptApp.getProjectTriggers().some(function(t) {
          return t.getHandlerFunction() === 'dailyShiftReminder';
        });
        if (!hasDaily) {
          ScriptApp.newTrigger('dailyShiftReminder').timeBased().everyDays(1).atHour(9).create();
          t3 = true;
        }
        return ContentService.createTextOutput('triggers ok: onEdit=' + (t1 ? '作成' : '既存')
          + ' attendanceCron=' + (t2 ? '作成' : '既存') + ' dailyReminder=' + (t3 ? '作成' : '既存'));
      }
      if (act === 'caldel') {
        // 検証用: 指定日の [shift-sync] イベントを全店舗分削除
        var dd = e.parameter.date || '';
        if (!/^\d{4}-\d{2}-\d{2}$/.test(dd)) return ContentService.createTextOutput('date required (YYYY-MM-DD)');
        var dres = deleteCalendarEvents_([dd]);
        return ContentService.createTextOutput('caldel ok: deleted=' + dres.deleted);
      }
      return ContentService.createTextOutput('unknown admin action');
    }
    if (e.parameter && e.parameter.payload) {
      var payload = JSON.parse(e.parameter.payload);
      handleSlackInteraction_(payload);
      return ContentService.createTextOutput(''); // Slackへの即ACK
    }
    return ContentService.createTextOutput('ok');
  } catch (err) {
    Logger.log('doPost error: ' + err.message);
    return ContentService.createTextOutput('error');
  }
}

function respondSlack_(responseUrl, message) {
  if (!responseUrl) return;
  UrlFetchApp.fetch(responseUrl, {
    method: 'post',
    contentType: 'application/json',
    payload: JSON.stringify(message),
    muteHttpExceptions: true,
  });
}

// state.values から action_id の選択値を取り出す
function pickStateValue_(payload, actionId) {
  var values = (payload.state && payload.state.values) || {};
  for (var blockId in values) {
    var block = values[blockId];
    if (block[actionId]) {
      var v = block[actionId];
      if (v.selected_option) return v.selected_option.value;
      if (v.selected_date) return v.selected_date;
      if (v.value) return v.value;
    }
  }
  return '';
}

function staffNameById_(staffId) {
  var staff = readStaffMaster_();
  for (var i = 0; i < staff.length; i++) {
    if (staff[i]['staff_id'] === staffId) return staff[i]['氏名'];
  }
  return '';
}

function staffSlackById_(staffId) {
  var staff = readStaffMaster_();
  for (var i = 0; i < staff.length; i++) {
    if (staff[i]['staff_id'] === staffId) return String(staff[i]['Slack ID'] || '');
  }
  return '';
}

function handleSlackInteraction_(payload) {
  var actions = payload.actions || [];
  if (!actions.length) return;
  var action = actions[0];
  var aid = action.action_id || '';
  var responseUrl = payload.response_url;

  // --- 希望入力: shift_<yyyy-MM-dd>_出勤 / shift_<yyyy-MM-dd>_希望休 ---
  var m = aid.match(/^shift_(\d{4}-\d{2}-\d{2})_(出勤|希望休)$/);
  if (m) {
    var dateStr = m[1];
    var kind = m[2];
    var staffId = action.value;
    var staffName = staffNameById_(staffId);

    if (kind === '希望休') {
      upsertWishData_(dateStr, staffId, staffName, '休み希望', '', '', 'slack');
      updateWishGridCell_(dateStr, staffName, '休み希望', '', '');
      respondSlack_(responseUrl, { replace_original: false, text: '🙏 ' + dateStr + ' を休み希望で登録しました' });
    } else {
      // 出勤 → 時間選択メッセージを返す
      var timeOpts = [];
      ['9:00','9:30','10:00','10:30','11:00','11:30','12:00','12:30','13:00','13:30','14:00','14:30','15:00','15:30','16:00','16:30','17:00','17:30','18:00','18:30','19:00','19:30','20:00','20:30','21:00','21:30','22:00','22:30','23:00','23:30','0:00','0:30'].forEach(function(t) {
        timeOpts.push({ text: { type: 'plain_text', text: t }, value: t });
      });
      respondSlack_(responseUrl, {
        replace_original: false,
        text: dateStr + ' の出勤時間を選択',
        blocks: [
          { type: 'section', text: { type: 'mrkdwn', text: '⏰ *' + dateStr + '* の出勤・退勤時間を選んで「確定」を押してください' } },
          { type: 'actions', block_id: 'wishtime_' + dateStr, elements: [
            { type: 'static_select', action_id: 'wishstart', placeholder: { type: 'plain_text', text: '出勤' }, options: timeOpts },
            { type: 'static_select', action_id: 'wishend', placeholder: { type: 'plain_text', text: '退勤' }, options: timeOpts },
            { type: 'button', text: { type: 'plain_text', text: '確定' }, style: 'primary', value: staffId + '|' + dateStr, action_id: 'wishconfirm' },
          ] },
        ],
      });
    }
    return;
  }

  // --- 希望入力: 出勤時間の確定 ---
  if (aid === 'wishconfirm') {
    var p = action.value.split('|');
    var staffId = p[0], dateStr = p[1];
    var start = pickStateValue_(payload, 'wishstart');
    var end = pickStateValue_(payload, 'wishend');
    if (!start || !end) {
      respondSlack_(responseUrl, { replace_original: false, text: '⚠ 出勤・退勤の両方を選んでから「確定」を押してください' });
      return;
    }
    var staffName = staffNameById_(staffId);
    upsertWishData_(dateStr, staffId, staffName, '出勤', start, end, 'slack');
    updateWishGridCell_(dateStr, staffName, '出勤', start, end);
    respondSlack_(responseUrl, { replace_original: true, text: '✅ ' + dateStr + ' 出勤 ' + start + '〜' + end + ' で登録しました' });
    return;
  }

  // --- 固定シフト: 希望休の日数選択 → datepicker N個 ---
  if (aid === 'fixedcount') {
    var p = (action.selected_option ? action.selected_option.value : '').split('|');
    var staffId = p[0], ps = p[1], pe = p[2], n = parseInt(p[3], 10) || 1;
    var blocks = [
      { type: 'section', text: { type: 'mrkdwn', text: '📅 希望休の日付を ' + n + '日分 選んで「送信」を押してください (' + ps + '〜' + pe + ')' } },
    ];
    for (var i = 0; i < n; i++) {
      blocks.push({ type: 'actions', block_id: 'fixdate_block_' + i, elements: [
        { type: 'datepicker', action_id: 'fixdate_' + i, placeholder: { type: 'plain_text', text: '希望休 ' + (i + 1) } },
      ] });
    }
    blocks.push({ type: 'actions', block_id: 'fixsubmit_block', elements: [
      { type: 'button', text: { type: 'plain_text', text: '📨 送信' }, style: 'primary', value: staffId + '|' + n, action_id: 'fixsubmit' },
    ] });
    respondSlack_(responseUrl, { replace_original: false, text: '希望休の日付選択', blocks: blocks });
    return;
  }

  // --- 固定シフト: 希望休の送信 ---
  if (aid === 'fixsubmit') {
    var p = action.value.split('|');
    var staffId = p[0], n = parseInt(p[1], 10) || 1;
    var staffName = staffNameById_(staffId);
    var dates = [];
    for (var i = 0; i < n; i++) {
      var d = pickStateValue_(payload, 'fixdate_' + i);
      if (d) dates.push(d);
    }
    if (!dates.length) {
      respondSlack_(responseUrl, { replace_original: false, text: '⚠ 日付を選んでから「送信」を押してください' });
      return;
    }
    dates.forEach(function(d) {
      upsertWishData_(d, staffId, staffName, '休み希望', '', '', 'slack');
      updateWishGridCell_(d, staffName, '休み希望', '', '');
    });
    respondSlack_(responseUrl, { replace_original: true, text: '🙏 希望休 ' + dates.length + '日分を登録しました: ' + dates.join(', ') });
    return;
  }

  // --- 固定シフト: 希望休なし ---
  if (aid === 'fixednone') {
    var p = action.value.split('|');
    var staffId = p[0], ps = p[1];
    var staffName = staffNameById_(staffId);
    upsertWishData_(ps, staffId, staffName, '希望休なし', '', '', 'slack'); // 提出済みマーカー
    respondSlack_(responseUrl, { replace_original: false, text: '✅ 「希望休なし」で受け付けました' });
    return;
  }

  // --- 勤怠: 通常出勤 ---
  if (aid === 'att_ok') {
    var p = action.value.split('|');
    if (!attendClickerOk_(payload, p[0], responseUrl)) return;
    kintaiRespond_(p[1], p[0], '時間通り', 0);
    respondSlack_(responseUrl, { replace_original: true,
      text: '🟢 ' + attendMention_(p[0]) + ' 出勤確認を受け付けました。本日もよろしくお願いします！' });
    return;
  }

  // --- 勤怠: 遅刻 → 分数選択 (ドロップダウン・5分刻み) ---
  if (aid === 'att_late') {
    var val = action.value; // staffId|date
    var staffId = val.split('|')[0];
    if (!attendClickerOk_(payload, staffId, responseUrl)) return;
    var opts = [];
    for (var mi = 5; mi <= 60; mi += 5) {
      opts.push({ text: { type: 'plain_text', text: mi + '分' }, value: val + '|' + mi });
    }
    respondSlack_(responseUrl, {
      replace_original: true,
      text: 'どのくらい遅れそうですか？',
      blocks: [
        { type: 'section', text: { type: 'mrkdwn', text: '🕐 ' + attendMention_(staffId) + ' どのくらい遅れそうですか？' } },
        { type: 'actions', block_id: 'attmin_block', elements: [
          { type: 'static_select', placeholder: { type: 'plain_text', text: '遅刻時間を選択' },
            options: opts, action_id: 'attmin_select' },
        ] },
      ],
    });
    return;
  }

  // --- 勤怠: 遅刻分数 (ドロップダウン選択。旧ボタン attmin_N も互換受理) ---
  if (aid === 'attmin_select' || aid.indexOf('attmin_') === 0) {
    var v = (action.selected_option && action.selected_option.value) || action.value;
    var p = v.split('|'); // staffId|date|min
    var staffId = p[0], dateStr = p[1], min = parseInt(p[2], 10) || 0;
    if (!attendClickerOk_(payload, staffId, responseUrl)) return;
    kintaiRespond_(dateStr, staffId, '遅刻', min);
    respondSlack_(responseUrl, { replace_original: true,
      text: '🕐 *遅刻連絡* ' + attendMention_(staffId) + ' ' + dateStr + ' 約' + min + '分遅刻で記録しました。気をつけてお越しください。' });
    return;
  }

  Logger.log('Unhandled action_id: ' + aid);
}

// --------------------------------------------------------------------------
// 3) 勤怠 (出勤確認・遅刻記録)
// --------------------------------------------------------------------------

function kintaiSheet_() {
  var ss = SpreadsheetApp.openById(SS_ID);
  var sh = ss.getSheetByName(SN_KINTAI);
  if (!sh) {
    sh = ss.insertSheet(SN_KINTAI);
    sh.getRange('A:K').setNumberFormat('@');
    sh.getRange(1, 1, 1, KINTAI_HEADERS.length).setValues([KINTAI_HEADERS]).setFontWeight('bold');
    sh.setFrozenRows(1);
    try { sh.hideSheet(); } catch (e) {} // 正本タブは非表示運用
  }
  return sh;
}

function kintaiFindRow_(dateStr, staffId) {
  var sh = kintaiSheet_();
  var last = sh.getLastRow();
  if (last < 2) return null;
  var vals = sh.getRange(2, 1, last - 1, KINTAI_HEADERS.length).getValues();
  for (var i = 0; i < vals.length; i++) {
    if (normDateStr_(vals[i][0]) === dateStr && String(vals[i][1]) === staffId) {
      return {
        row: i + 2,
        notified_at: String(vals[i][5] || ''),
        renotified_at: String(vals[i][6] || ''),
        response: String(vals[i][7] || ''),
        alerted: String(vals[i][10] || ''),
      };
    }
  }
  return null;
}

function kintaiRespond_(dateStr, staffId, response, lateMinutes) {
  var found = kintaiFindRow_(dateStr, staffId);
  var now = Utilities.formatDate(new Date(), Session.getScriptTimeZone(), 'HH:mm');
  var sh = kintaiSheet_();
  if (found) {
    sh.getRange(found.row, 8, 1, 3).setValues([[response, lateMinutes ? String(lateMinutes) : '', now]]);
  } else {
    // 通知前の応答は通常ないが念のため行を作る
    sh.getRange(sh.getLastRow() + 1, 1, 1, KINTAI_HEADERS.length)
      .setValues([[dateStr, staffId, staffNameById_(staffId), '', '', '', '', response, lateMinutes ? String(lateMinutes) : '', now, '']]);
  }
}

/**
 * 出勤確認クロン (10分ごとの時間トリガーで実行)
 *  - 出勤 ATTEND_NOTIFY_MIN 分前 (デフォルト60): #shift-management にメンション付き出勤確認を投稿
 *  - 出勤 ATTEND_RENOTIFY_MIN 分前 (デフォルト30): 未応答なら再投稿
 *  - 出勤時刻経過: 未応答なら同チャンネルへアラート
 */
function attendanceCron() {
  var tz = Session.getScriptTimeZone();
  var now = new Date();
  var today = Utilities.formatDate(now, tz, 'yyyy-MM-dd');
  var nowMin = now.getHours() * 60 + now.getMinutes();

  var entries = readShiftDataRange_(today, today).filter(function(r) {
    return r.shift_status === '出勤' && r.start_time && r.staff_id;
  });
  if (!entries.length) return;

  // スタッフごとに最も早い出勤を採用 (複数店舗兼務対応)
  var byStaff = {};
  entries.forEach(function(r) {
    var t = r.start_time.split(':');
    var startMin = parseInt(t[0], 10) * 60 + parseInt(t[1] || 0, 10);
    if (!byStaff[r.staff_id] || startMin < byStaff[r.staff_id].startMin) {
      byStaff[r.staff_id] = { entry: r, startMin: startMin };
    }
  });

  var staff = readStaffMaster_();
  var slackBySid = {};
  staff.forEach(function(s) { slackBySid[s['staff_id']] = s['Slack ID'] || ''; });

  var notifyMin = parseInt(getProp_('ATTEND_NOTIFY_MIN') || '60', 10);
  var renotifyMin = parseInt(getProp_('ATTEND_RENOTIFY_MIN') || '30', 10);
  var nowStr = Utilities.formatDate(now, tz, 'HH:mm');

  for (var sid in byStaff) {
    var r = byStaff[sid].entry;
    var diff = byStaff[sid].startMin - nowMin;
    var slackId = slackBySid[sid];
    var kint = kintaiFindRow_(today, sid);

    if (!kint && diff > 0 && diff <= notifyMin) {
      // 初回通知 (チャンネル投稿・Slack ID未登録者は氏名表記)
      sendAttendanceNotice_(slackId, sid, r, today);
      var sh = kintaiSheet_();
      sh.getRange(sh.getLastRow() + 1, 1, 1, KINTAI_HEADERS.length)
        .setValues([[today, sid, r.staff_name, r.store, r.start_time, nowStr, '', '', '', '', '']]);
    } else if (kint && !kint.response && !kint.renotified_at && diff > 0 && diff <= renotifyMin) {
      // 再通知
      sendAttendanceNotice_(slackId, sid, r, today);
      kintaiSheet_().getRange(kint.row, 7).setValue(nowStr);
    } else if (kint && !kint.response && !kint.alerted && diff <= 0) {
      // 未応答アラート
      slackPost_('⚠ *出勤確認 未応答* ' + r.staff_name + ' さん (' + r.store + ' ' + r.start_time + '〜) が出勤確認に応答していません');
      var sh2 = kintaiSheet_();
      sh2.getRange(kint.row, 8).setValue('未応答');
      sh2.getRange(kint.row, 11).setValue('yes');
    }
  }
}

// 出勤確認は #shift-management チャンネルにメンション付きで投稿 (2026-07-06 DM方式から変更)
function sendAttendanceNotice_(slackId, staffId, entry, dateStr) {
  var mention = slackId ? '<@' + slackId + '>' : entry.staff_name + ' さん';
  var blocks = [
    { type: 'section', text: { type: 'mrkdwn',
      text: mention + '\n本日 *' + entry.store + ' ' + entry.start_time + '〜' + (entry.end_time || '') + '* の出勤です。\n出勤確認をお願いします。' } },
    { type: 'actions', block_id: 'attend_' + dateStr, elements: [
      { type: 'button', text: { type: 'plain_text', text: '🟢 通常出勤' }, style: 'primary', value: staffId + '|' + dateStr, action_id: 'att_ok' },
      { type: 'button', text: { type: 'plain_text', text: '🕐 遅刻' }, style: 'danger', value: staffId + '|' + dateStr, action_id: 'att_late' },
    ] },
  ];
  slackPost_('【出勤確認】' + dateStr + ' ' + entry.store + ' ' + entry.start_time + '〜', blocks);
}

// 出勤確認ボタンのメンション表記 (Slack ID未登録者は氏名)
function attendMention_(staffId) {
  var slackId = staffSlackById_(staffId);
  return slackId ? '<@' + slackId + '>' : staffNameById_(staffId) + ' さん';
}

// チャンネル方式のため本人 (または管理者) 以外の操作を拒否
function attendClickerOk_(payload, staffId, responseUrl) {
  var clicker = (payload.user && payload.user.id) || '';
  var target = staffSlackById_(staffId);
  var admin = getProp_('SLACK_ADMIN_ID') || 'U07UBN61QFN';
  if (target && clicker && clicker !== target && clicker !== admin) {
    respondSlack_(responseUrl, { response_type: 'ephemeral', replace_original: false,
      text: '⚠ この出勤確認は本人のみ操作できます' });
    return false;
  }
  return true;
}

// 勤怠クロンのトリガーを設定 (メニュー⑥から)
function ensureAttendanceTrigger_() {
  var triggers = ScriptApp.getProjectTriggers();
  for (var i = 0; i < triggers.length; i++) {
    if (triggers[i].getHandlerFunction() === 'attendanceCron') return false;
  }
  ScriptApp.newTrigger('attendanceCron').timeBased().everyMinutes(10).create();
  return true;
}

// --------------------------------------------------------------------------
// 4) 個人シフト (A1=年 / A2=月 / A3=名前 セレクター)
// --------------------------------------------------------------------------

// ユーザー設計のレイアウト: A1=年 / A2=月 / C1='名前'ラベル / C2=名前(ドロップダウン)
// Row3 = ヘッダー (日付|曜日|店舗|時間|勤怠) / Row4以降 = データ
var PERSONAL_HEADER_ROW = 3;

function renderPersonalShift_() {
  var sh = sheet_(SN_PERSONAL);
  if (!sh) return;
  var year = String(sh.getRange(1, 1).getValue()).trim();
  var month = String(sh.getRange(2, 1).getValue()).trim();
  var name = String(sh.getRange(2, 3).getValue()).trim(); // C2=名前
  if (!year || !month || !name) return;

  // 名前セレクター(C2)のドロップダウンを最新化
  var staff = readStaffMaster_();
  var names = staff.map(function(s) { return s['氏名']; });
  sh.getRange(2, 3).setDataValidation(
    SpreadsheetApp.newDataValidation().requireValueInList(names, true).setAllowInvalid(true).build());

  var m = ('0' + month).slice(-2);
  var lastDay = new Date(parseInt(year), parseInt(month), 0).getDate();
  var firstDate = year + '-' + m + '-01';
  var lastDate = year + '-' + m + '-' + ('0' + lastDay).slice(-2);

  var shifts = readShiftDataRange_(firstDate, lastDate).filter(function(r) { return r.staff_name === name; });
  var byDate = {};
  shifts.forEach(function(r) {
    if (!byDate[r.date]) byDate[r.date] = [];
    byDate[r.date].push(r);
  });

  // 勤怠実績
  var kintaiByDate = {};
  var ksh = kintaiSheet_();
  var klast = ksh.getLastRow();
  if (klast >= 2) {
    var kvals = ksh.getRange(2, 1, klast - 1, KINTAI_HEADERS.length).getValues();
    kvals.forEach(function(kr) {
      var d = normDateStr_(kr[0]);
      if (d < firstDate || d > lastDate) return;
      if (String(kr[2] || '') !== name) return;
      var resp = String(kr[7] || '');
      var late = String(kr[8] || '');
      kintaiByDate[d] = resp === '遅刻' ? '🕐 遅刻 ' + late + '分' : resp === '時間通り' ? '🟢 時間通り' : resp === '未応答' ? '⚠ 未応答' : '';
    });
  }

  // ヘッダー (日付|曜日|店舗|時間|勤怠) + データ描画
  sh.getRange(PERSONAL_HEADER_ROW, 1, 1, 5)
    .setValues([['日付', '曜日', '店舗', '時間', '勤怠']])
    .setFontWeight('bold')
    .setBackground('#D9EBF7')
    .setFontColor('#202124')
    .setHorizontalAlignment('center');
  // 過去の描画残骸も含めて広めにクリア (F列の迷いテキスト等)
  sh.getRange(PERSONAL_HEADER_ROW + 1, 1, 40, 8)
    .clearContent().setBackground(null).setFontWeight('normal').setFontColor('#202124');

  var rows = [];
  var bgs = [];
  var workDays = 0;
  var lateCount = 0, lateMin = 0;
  for (var day = 1; day <= lastDay; day++) {
    var dateStr = year + '-' + m + '-' + ('0' + day).slice(-2);
    var d = new Date(parseInt(year), parseInt(month) - 1, day);
    var dow = DOW_JP[d.getDay()];
    var dayShifts = byDate[dateStr] || [];
    var work = dayShifts.filter(function(r) { return r.shift_status === '出勤'; });
    var bg = (d.getDay() === 0) ? '#F4C7C3' : (d.getDay() === 6) ? '#B4D7F0' : '#FFFFFF';

    var kint = kintaiByDate[dateStr] || '';
    if (kint.indexOf('遅刻') >= 0) {
      lateCount++;
      var lm = kint.match(/(\d+)分/);
      if (lm) lateMin += parseInt(lm[1], 10);
    }

    if (work.length) {
      workDays++;
      var stores = work.map(function(r) { return r.store; }).join(', ');
      var times = work.map(function(r) { return (r.start_time || '') + '-' + (r.end_time || ''); }).join(', ');
      rows.push([(parseInt(month)) + '/' + day, dow, stores, times, kint]);
    } else {
      rows.push([(parseInt(month)) + '/' + day, dow, '公休', '', kint]);
    }
    bgs.push([bg, bg, bg, bg, bg]);
  }
  sh.getRange(PERSONAL_HEADER_ROW + 1, 1, rows.length, 5).setValues(rows).setBackgrounds(bgs);
  // 公休はグレー文字、出勤日は黒
  for (var i = 0; i < rows.length; i++) {
    sh.getRange(PERSONAL_HEADER_ROW + 1 + i, 3, 1, 2)
      .setFontColor(rows[i][2] === '公休' ? '#9AA0A6' : '#202124');
  }
  // 表全体の罫線
  sh.getRange(PERSONAL_HEADER_ROW, 1, rows.length + 1, 5)
    .setBorder(true, true, true, true, true, true, '#DADCE0', SpreadsheetApp.BorderStyle.SOLID);
  // サマリー行 (出勤日数 + 遅刻集計)
  sh.getRange(PERSONAL_HEADER_ROW + 1 + rows.length, 1, 1, 5)
    .setValues([['出勤日数', String(workDays) + '日', '遅刻', lateCount + '回 / ' + lateMin + '分', '']])
    .setFontWeight('bold')
    .setBackground('#E8EAED');
}

// 個人シフトシートの初期構築 (存在しなければ作成、ユーザー設計のセレクター配置)
function ensurePersonalSheet_() {
  var sh = sheet_(SN_PERSONAL);
  if (!sh) {
    sh = ss_().insertSheet(SN_PERSONAL);
  }
  if (!String(sh.getRange(1, 2).getValue()).trim()) {
    var now = new Date();
    sh.getRange(1, 1).setValue(now.getFullYear());
    sh.getRange(1, 2).setValue('年');
    sh.getRange(2, 1).setValue(now.getMonth() + 1);
    sh.getRange(2, 2).setValue('月');
    sh.getRange(1, 3).setValue('名前');
  }
  // セレクター: A1=年 / A2=月 / C2=名前 すべてドロップダウン
  var thisYear = new Date().getFullYear();
  var years = [];
  for (var y = thisYear - 1; y <= thisYear + 2; y++) years.push(String(y));
  sh.getRange(1, 1).setDataValidation(
    SpreadsheetApp.newDataValidation().requireValueInList(years, true).setAllowInvalid(true).build());
  var months = [];
  for (var mo = 1; mo <= 12; mo++) months.push(String(mo));
  sh.getRange(2, 1).setDataValidation(
    SpreadsheetApp.newDataValidation().requireValueInList(months, true).setAllowInvalid(true).build());

  var staff = readStaffMaster_();
  var names = staff.map(function(s) { return s['氏名']; });
  sh.getRange(2, 3).setDataValidation(
    SpreadsheetApp.newDataValidation().requireValueInList(names, true).setAllowInvalid(true).build());

  // セレクターの見た目 (3シート共通デザイン)
  styleYmSelector_(sh, 2);
  sh.getRange(2, 3).setBackground('#FFF2CC').setFontWeight('bold');
  sh.getRange(1, 3).setFontWeight('bold');

  // 列幅
  try {
    sh.setColumnWidth(1, 56);   // 日付
    sh.setColumnWidth(2, 40);   // 曜日
    sh.setColumnWidth(3, 110);  // 店舗
    sh.setColumnWidth(4, 140);  // 時間
    sh.setColumnWidth(5, 120);  // 勤怠
  } catch (e) {}
  return sh;
}
