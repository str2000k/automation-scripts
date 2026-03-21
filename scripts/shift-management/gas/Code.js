/**
 * シフト管理システム - Google Apps Script
 *
 * スプレッドシート: 1JlyWngnuha1IHQLMGs5bzTnjct8s7eY4Z-bW0YHIlmU
 *
 * メニュー「シフト管理」:
 *   ① スタッフマスタ同期: スタッフマスタ → 希望収集データヘッダー + kintone 213
 *   ② 希望シフト取得: kintone 211 → 希望収集データ (年月セレクター連動)
 *   ③ AIシフト生成: Claude API → シフト出力シート
 *   ④ 確定シフト反映: チェック済み行 → kintone 212 + Google Calendar
 *   ⑤ 月データ読込: 年月セレクターの値でkintoneからデータを両シートに表示
 *
 * シフト出力 横型レイアウト (40列):
 *   Row 1: 年月セレクター (D1=年[D:E merged], F1="年", G1=月[G:H merged], I1="月")
 *   Row 2: 店舗名ヘッダー (藤沢/伊勢佐木町/新宿/工場/本部オフィス/メモ)
 *   Row 3: シフト種別 (営業: 早番/遅番/※予備) / 担当者名 (非営業)
 *   Row 4: カラムヘッダー (確定/変更/日付/出勤/退勤...)
 *   Row 5+: データ (2行/日 × 31日 = 62行)
 *     - 日付行(名前行): 営業=名前プルダウン, 非営業=出勤/公休プルダウン
 *     - 曜日行(時間行): 出勤/退勤の時間プルダウン (6:00-25:00)
 *
 *   Columns (1-indexed, total=40):
 *     A(1)=確定  B(2)=変更  C(3)=日付
 *     D-I(4-9):    藤沢      (早番[出,退], 遅番[出,退], ※予備[出,退])
 *     J-O(10-15):  伊勢佐木町 (早番[出,退], 遅番[出,退], ※予備[出,退])
 *     P-U(16-21):  新宿      (早番[出,退], ※予備[出,退], ※予備[出,退])
 *     V-AA(22-27): 工場       (3人 × [出勤,退勤])
 *     AB-AM(28-39):本部オフィス (6人 × [出勤,退勤])
 *     AN(40):      メモ
 *
 * installable onEdit trigger:
 *   onEditTrigger(e) → 年月変更時にkintoneからデータ自動読込
 *   設定: Apps Script → トリガー → onEditTrigger / スプレッドシートから / 編集時
 *
 * Script Properties:
 *   KINTONE_DOMAIN, KINTONE_USERNAME, KINTONE_PASSWORD
 *   ANTHROPIC_API_KEY, SLACK_BOT_TOKEN, SLACK_SHIFT_CHANNEL, SHIFT_CALENDAR_ID
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
var OUTPUT_HROWS = 4;
var OUTPUT_DSTART = 5; // 1-indexed first data row
var OUTPUT_TOTAL_COLS = 40;
var OUTPUT_DAYS = 31;

// Year/month selector positions (1-indexed)
var YEAR_COL = 4;   // D1 (merged D1:E1)
var MONTH_COL = 7;  // G1 (merged G1:H1)

// Retail store column map (1-indexed)
// Each shift type: {n: name cell col, s: 出勤 time col, e: 退勤 time col}
var RETAIL_COLS = {
  '藤沢':       { '早番': {n:4,s:4,e:5},   '遅番': {n:6,s:6,e:7},   '※予備': {n:8,s:8,e:9} },
  '伊勢佐木町': { '早番': {n:10,s:10,e:11}, '遅番': {n:12,s:12,e:13}, '※予備': {n:14,s:14,e:15} },
  '新宿':       { '早番': {n:16,s:16,e:17}, '遅番': {n:18,s:18,e:19}, '※予備': {n:20,s:20,e:21} },
};
var SHIFT_TYPES = ['早番', '遅番', '※予備'];

// Non-retail person columns (1-indexed): each person = {s: 出勤 col, e: 退勤 col}
var FACTORY_PERSON_COLS = [
  {s:22, e:23},  // V,W
  {s:24, e:25},  // X,Y
  {s:26, e:27},  // Z,AA
];
var OFFICE_PERSON_COLS = [
  {s:28, e:29},  // AB,AC
  {s:30, e:31},  // AD,AE
  {s:32, e:33},  // AF,AG
  {s:34, e:35},  // AH,AI
  {s:36, e:37},  // AJ,AK
  {s:38, e:39},  // AL,AM
];

// Default person names (updated by ① syncStaffMaster)
var FACTORY_NAMES = ['髙橋舞人', '牧田健之介', ''];
var OFFICE_NAMES  = ['小林智', '大塚裕斗', '工藤稜也', '松本明日香', '西村瑠依', ''];

// ==========================================================================
// Menu
// ==========================================================================

function onOpen() {
  SpreadsheetApp.getUi().createMenu('シフト管理')
    .addItem('① スタッフマスタ同期', 'syncStaffMaster')
    .addItem('② 希望シフト取得', 'fetchWishData')
    .addItem('③ AIシフト生成', 'generateShift')
    .addItem('④ 確定シフト反映', 'syncConfirmedShift')
    .addSeparator()
    .addItem('⑤ 月データ読込 (年月セレクター)', 'loadMonthData')
    .addToUi();
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
  var name = sheet.getName();
  var row = e.range.getRow();
  var col = e.range.getColumn();

  // Year selector = D1 (col 4), Month selector = G1 (col 7)
  if (row !== 1) return;

  if (name === SN_OUTPUT && (col === YEAR_COL || col === MONTH_COL)) {
    var year = String(sheet.getRange(1, YEAR_COL).getValue());
    var month = String(sheet.getRange(1, MONTH_COL).getValue());
    if (year && month) {
      loadShiftOutputData_(year, month);
    }
  } else if (name === SN_WISH && (col === YEAR_COL || col === MONTH_COL)) {
    var year = String(sheet.getRange(1, YEAR_COL).getValue());
    var month = String(sheet.getRange(1, MONTH_COL).getValue());
    if (year && month) {
      loadWishSheetData_(year, month);
    }
  }
}

/**
 * ⑤ 月データ読込 (メニューから手動実行)
 */
function loadMonthData() {
  var ui = SpreadsheetApp.getUi();
  try {
    var outputSheet = sheet_(SN_OUTPUT);

    var year = String(outputSheet.getRange(1, YEAR_COL).getValue()).trim();
    var month = String(outputSheet.getRange(1, MONTH_COL).getValue()).trim();
    if (!year || !month) { ui.alert('年月セレクター(D1/G1)を設定してください'); return; }

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

  // Clear data area (keep headers and date formulas in C)
  outputSheet.getRange(OUTPUT_DSTART, 1, OUTPUT_DAYS * 2, 2).clearContent();  // A-B checkboxes
  outputSheet.getRange(OUTPUT_DSTART, 4, OUTPUT_DAYS * 2, OUTPUT_TOTAL_COLS - 3).clearContent(); // D-AN data

  // Fetch kintone 212
  var query = 'shift_date >= "' + firstDate + '" and shift_date <= "' + lastDate
    + '" order by shift_date asc, staff_id asc limit 500';
  var result = kintoneGet_(KINTONE_CONFIRMED_APP, query);
  var records = result.records || [];
  if (!records.length) return '0件';

  // Read non-retail person names from Row 3 header
  var factoryNames = readNonRetailHeaders_(outputSheet, FACTORY_PERSON_COLS);
  var officeNames = readNonRetailHeaders_(outputSheet, OFFICE_PERSON_COLS);

  // Build day lookup: {dayNum: [{record data}]}
  var dayData = {};
  records.forEach(function(r) {
    var dateStr = r.shift_date ? r.shift_date.value : '';
    if (!dateStr) return;
    var day = parseInt(dateStr.split('-')[2], 10);
    if (!dayData[day]) dayData[day] = [];
    dayData[day].push({
      staff_name: r.staff_name ? r.staff_name.value : '',
      store: r.store ? r.store.value : '',
      start_time: r.start_time ? r.start_time.value : '',
      end_time: r.end_time ? r.end_time.value : '',
      shift_type: r.shift_type ? r.shift_type.value : '',
      shift_status: r.shift_status ? r.shift_status.value : '出勤',
    });
  });

  // Write data
  var written = 0;
  for (var day = 1; day <= lastDay; day++) {
    var entries = dayData[day] || [];
    var nameRowNum = OUTPUT_DSTART + (day - 1) * 2;
    var timeRowNum = nameRowNum + 1;

    entries.forEach(function(entry) {
      var store = entry.store;
      var stype = entry.shift_type;

      // Retail stores
      if (RETAIL_COLS[store] && RETAIL_COLS[store][stype]) {
        var cols = RETAIL_COLS[store][stype];
        outputSheet.getRange(nameRowNum, cols.n).setValue(entry.staff_name);
        if (entry.start_time) outputSheet.getRange(timeRowNum, cols.s).setValue(entry.start_time);
        if (entry.end_time) outputSheet.getRange(timeRowNum, cols.e).setValue(entry.end_time);
        written++;
        return;
      }

      // Non-retail: find person column by name
      var personCols, personNames;
      if (store === '工場') {
        personCols = FACTORY_PERSON_COLS;
        personNames = factoryNames;
      } else if (store === '本部オフィス') {
        personCols = OFFICE_PERSON_COLS;
        personNames = officeNames;
      } else {
        return;
      }

      var idx = personNames.indexOf(entry.staff_name);
      if (idx < 0) {
        for (var i = 0; i < personNames.length; i++) {
          if (!personNames[i]) { idx = i; personNames[i] = entry.staff_name; break; }
        }
      }
      if (idx >= 0 && idx < personCols.length) {
        var pc = personCols[idx];
        if (entry.shift_status === '公休') {
          outputSheet.getRange(nameRowNum, pc.s).setValue('公休');
        } else {
          outputSheet.getRange(nameRowNum, pc.s).setValue('出勤');
          if (entry.start_time) outputSheet.getRange(timeRowNum, pc.s).setValue(entry.start_time);
          if (entry.end_time) outputSheet.getRange(timeRowNum, pc.e).setValue(entry.end_time);
        }
        written++;
      }
    });
  }

  return records.length + '件読込 (' + written + '配置)';
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

  // Clear data area (Row 4+, cols C onward)
  var clearCols = Math.max(lastCol, 12);
  if (clearCols > 2) {
    wishSheet.getRange(4, 3, 31, clearCols - 2).clearContent();
  }

  // Fetch kintone 211
  var query = 'shift_date >= "' + firstDate + '" and shift_date <= "' + lastDate
    + '" and input_status in ("入力済")'
    + ' order by shift_date asc limit 500';
  var result = kintoneGet_(KINTONE_WISH_APP, query);
  var records = result.records || [];
  if (!records.length) return '0件';

  // Build lookup
  var wishes = {};
  records.forEach(function(r) {
    var sid = r.staff_id ? r.staff_id.value : '';
    var dateStr = r.shift_date ? r.shift_date.value : '';
    if (!sid || !dateStr) return;
    var day = parseInt(dateStr.split('-')[2], 10);
    if (!wishes[sid]) wishes[sid] = {};
    wishes[sid][day] = {
      shift_type: r.shift_type ? r.shift_type.value : '出勤',
      start_time: r.start_time ? r.start_time.value : '',
      end_time: r.end_time ? r.end_time.value : '',
    };
  });

  // Write wish staff data (出勤/退勤)
  var written = 0;
  for (var si = 0; si < wishStaffNames.length; si++) {
    var sname = wishStaffNames[si];
    var sid = nameToSid[sname];
    if (!sid) continue;
    var sw = wishes[sid] || {};

    for (var day = 1; day <= lastDay; day++) {
      var dw = sw[day];
      if (!dw) continue;

      var row = day + 3;  // Row 4 = day 1
      var startCol = wishStaffCols[si] + 1;  // 1-indexed
      var endCol = startCol + 1;

      if (dw.shift_type === '休み' || dw.shift_type === '希望休') {
        wishSheet.getRange(row, startCol).setValue('×');
      } else {
        if (dw.start_time) wishSheet.getRange(row, startCol).setValue(dw.start_time);
        if (dw.end_time) wishSheet.getRange(row, endCol).setValue(dw.end_time);
      }
      written++;
    }
  }

  // Write fixed staff data (希望休)
  for (var si = 0; si < fixedStaffNames.length; si++) {
    var sname = fixedStaffNames[si];
    var sid = nameToSid[sname];
    if (!sid) continue;
    var sw = wishes[sid] || {};

    for (var day = 1; day <= lastDay; day++) {
      var dw = sw[day];
      if (!dw) continue;

      var row = day + 3;
      var col = fixedStaffCols[si] + 1;  // 1-indexed

      if (dw.shift_type === '休み' || dw.shift_type === '希望休') {
        wishSheet.getRange(row, col).setValue('希望休');
      }
      written++;
    }
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
  'ANTHROPIC_API_KEY': '',
  'SLACK_BOT_TOKEN': '',
  'SLACK_SHIFT_CHANNEL': 'C0AKBJ1LTV2',
  'SHIFT_CALENDAR_ID': '',
};

function getProp_(key) {
  var val = PropertiesService.getScriptProperties().getProperty(key);
  return val || CONFIG_DEFAULTS_[key] || '';
}

function ss_() { return SpreadsheetApp.openById(SS_ID); }
function sheet_(name) { return ss_().getSheetByName(name); }

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
  var year = String(s.getRange(1, YEAR_COL).getValue()).trim();
  var month = String(s.getRange(1, MONTH_COL).getValue()).trim();
  if (!year || !month) return null;
  return { year: year, month: month };
}

/** Read non-retail person names from Row 3 header */
function readNonRetailHeaders_(sheet, personCols) {
  return personCols.map(function(pc) {
    return String(sheet.getRange(3, pc.s).getValue()).trim();
  });
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

// ==========================================================================
// Sheet reader helpers
// ==========================================================================

function readStaffMaster_() {
  var rows = sheetData_(SN_STAFF);
  if (rows.length < 2) return [];
  var header = rows[0];
  var storeNames = ['藤沢', '伊勢佐木町', '新宿', '工場', '本部オフィス'];
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
  var ui = SpreadsheetApp.getUi();
  try {
    var staff = readStaffMaster_();
    if (!staff.length) { ui.alert('スタッフマスタにデータがありません'); return; }

    // --- 希望収集データのヘッダー更新 ---
    // Layout: Row 1=年月セレクター(触らない), Row 2=スタッフ名, Row 3=サブヘッダー, Row 4+=データ(触らない)
    // Left: 希望シフト (2列/人: 出勤+退勤), Right: 固定シフト (1列/人: 希望休)
    var wishStaffCount = updateWishSheetHeaders_(staff);

    // --- kintone 213 同期 ---
    var existing = kintoneGet_(KINTONE_STAFF_APP, 'order by staff_id asc limit 500');
    var existMap = {};
    (existing.records || []).forEach(function(r) {
      existMap[r.staff_id.value] = r['$id'].value;
    });

    var toAdd = [];
    var toUpdate = [];
    staff.forEach(function(s) {
      var record = {
        staff_id: { value: s['staff_id'] },
        staff_name: { value: s['氏名'] },
        employment_type: { value: s['雇用形態'] },
        assigned_stores: { value: s['対応店舗'] },
        work_style: { value: s['働き方'] },
        position: { value: s['役職'] },
        max_weekly_hours: { value: s['最大労働時間/週'] || '40' },
        active: { value: s['有効フラグ'] },
      };
      var rid = existMap[s['staff_id']];
      if (rid) {
        toUpdate.push({ id: rid, record: record });
      } else {
        toAdd.push(record);
      }
    });

    var addCount = 0, updateCount = 0;
    for (var i = 0; i < toAdd.length; i += 100) {
      kintonePost_(KINTONE_STAFF_APP, toAdd.slice(i, i + 100));
      addCount += Math.min(100, toAdd.length - i);
    }
    for (var i = 0; i < toUpdate.length; i += 100) {
      kintonePut_(KINTONE_STAFF_APP, toUpdate.slice(i, i + 100));
      updateCount += Math.min(100, toUpdate.length - i);
    }

    // --- シフト出力の名前ドロップダウン更新 ---
    var nameDropdownCount = updateShiftNameDropdowns_(staff);

    ui.alert(
      'スタッフマスタ同期完了\n\n' +
      '希望収集データ: ' + wishStaffCount + '名のヘッダーを更新\n' +
      'シフト出力: 名前ドロップダウン ' + nameDropdownCount + '箇所更新\n' +
      'kintone 213: 新規 ' + addCount + '件 / 更新 ' + updateCount + '件'
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

  var wishStaff = staff.filter(function(s) {
    return s['働き方'] === '希望シフト' || s['働き方'] === '混合';
  });
  var fixedStaff = staff.filter(function(s) {
    return s['働き方'] === '固定シフト';
  });

  var nWish = wishStaff.length;
  var nFixed = fixedStaff.length;
  var wishColCount = nWish * 2;           // 出勤+退勤で2列/人
  var totalDataCols = wishColCount + nFixed;
  var totalCols = 2 + totalDataCols;      // A,B + data

  // -- Step 1: Row 2 全体をリセット (breakApart → clearContent) --
  var currentLastCol = wishSheet.getLastColumn();
  var clearWidth = Math.max(currentLastCol, totalCols);
  if (clearWidth > 2) {
    var r2range = wishSheet.getRange(2, 3, 1, clearWidth - 2);
    r2range.breakApart();
    r2range.clearContent();
    r2range.clearFormat();
    var r3range = wishSheet.getRange(3, 3, 1, clearWidth - 2);
    r3range.clearContent();
    r3range.clearFormat();
  }

  // -- Step 2: Row 2 の値を構築 --
  var row2vals = [];
  for (var i = 0; i < nWish; i++) {
    row2vals.push(wishStaff[i]['氏名']);
    row2vals.push('');
  }
  for (var i = 0; i < nFixed; i++) {
    row2vals.push(fixedStaff[i]['氏名']);
  }

  // -- Step 3: Row 3 の値を構築 --
  var row3vals = [];
  for (var i = 0; i < nWish; i++) {
    row3vals.push('出勤');
    row3vals.push('退勤');
  }
  for (var i = 0; i < nFixed; i++) {
    row3vals.push('希望休');
  }

  // -- Step 4: 値を書き込み --
  if (row2vals.length > 0) {
    wishSheet.getRange(2, 3, 1, row2vals.length).setValues([row2vals]);
  }
  if (row3vals.length > 0) {
    wishSheet.getRange(3, 3, 1, row3vals.length).setValues([row3vals]);
  }

  // -- Step 5: 希望シフトスタッフの2列結合 --
  for (var i = 0; i < nWish; i++) {
    var col = 3 + i * 2; // 1-indexed
    wishSheet.getRange(2, col, 1, 2).merge();
  }

  // -- Step 6: A2:B2, A3:B3 を書き込み (固定列ラベル) --
  wishSheet.getRange(2, 1).setValue('');
  wishSheet.getRange(2, 2).setValue('');
  wishSheet.getRange(3, 1).setValue('日付');
  wishSheet.getRange(3, 2).setValue('曜日');

  // -- Step 7: スタイリング --
  // Row 2 全体: 中央揃え、太字
  var row2all = wishSheet.getRange(2, 1, 1, totalCols);
  row2all.setFontWeight('bold').setHorizontalAlignment('center').setVerticalAlignment('middle').setFontSize(10);

  // Row 2 A:B を青ヘッダー
  wishSheet.getRange(2, 1, 1, 2).setBackground('#D9EBF7');

  // Row 2 希望シフト部分 (C〜): 青
  if (wishColCount > 0) {
    wishSheet.getRange(2, 3, 1, wishColCount).setBackground('#D9EBF7');
  }

  // Row 2 固定シフト部分: 緑
  if (nFixed > 0) {
    wishSheet.getRange(2, 3 + wishColCount, 1, nFixed).setBackground('#D9EAD3');
  }

  // Row 3: グレー、太字、中央
  wishSheet.getRange(3, 1, 1, totalCols)
    .setFontWeight('bold').setHorizontalAlignment('center').setVerticalAlignment('middle')
    .setFontSize(8).setBackground('#E8EAED');

  Logger.log('updateWishSheetHeaders_: wish=' + nWish + ', fixed=' + nFixed + ', totalCols=' + totalCols);
  return nWish + nFixed;
}

// ==========================================================================
// シフト出力 名前ドロップダウン更新 (横型レイアウト)
// ==========================================================================

function updateShiftNameDropdowns_(staff) {
  var outputSheet = sheet_(SN_OUTPUT);
  if (!outputSheet) return 0;

  var activeNames = staff.map(function(s) { return s['氏名']; });
  activeNames.unshift('');

  var rule = SpreadsheetApp.newDataValidation()
    .requireValueInList(activeNames, true)
    .setAllowInvalid(true)
    .build();

  var count = 0;

  for (var d = 0; d < OUTPUT_DAYS; d++) {
    var nameRowNum = OUTPUT_DSTART + d * 2;

    // Retail stores: merged name cells
    for (var store in RETAIL_COLS) {
      var shifts = RETAIL_COLS[store];
      for (var st = 0; st < SHIFT_TYPES.length; st++) {
        var cols = shifts[SHIFT_TYPES[st]];
        outputSheet.getRange(nameRowNum, cols.n).setDataValidation(rule);
        count++;
      }
    }
  }

  // Update non-retail person name headers in Row 3
  var factoryStaff = staff.filter(function(s) { return s['メイン店舗'] === '工場'; });
  var officeStaff = staff.filter(function(s) { return s['メイン店舗'] === '本部オフィス'; });

  for (var i = 0; i < FACTORY_PERSON_COLS.length; i++) {
    var name = i < factoryStaff.length ? factoryStaff[i]['氏名'] : '';
    outputSheet.getRange(3, FACTORY_PERSON_COLS[i].s).setValue(name);
  }
  for (var i = 0; i < OFFICE_PERSON_COLS.length; i++) {
    var name = i < officeStaff.length ? officeStaff[i]['氏名'] : '';
    outputSheet.getRange(3, OFFICE_PERSON_COLS[i].s).setValue(name);
  }

  return count;
}

// ==========================================================================
// ② 希望シフト取得
// ==========================================================================

function fetchWishData() {
  var ui = SpreadsheetApp.getUi();
  try {
    var ym = getSelectedYearMonth_();
    if (!ym) { ui.alert('年月セレクター(D1/G1)を設定してください'); return; }

    var year = ym.year;
    var month = ym.month;
    var m = ('0' + month).slice(-2);
    var firstDate = year + '-' + m + '-01';
    var lastDay = new Date(parseInt(year), parseInt(month), 0).getDate();
    var lastDate = year + '-' + m + '-' + ('0' + lastDay).slice(-2);

    // Sync wish sheet year/month (D1/G1)
    var wishSheet = sheet_(SN_WISH);
    if (wishSheet) {
      wishSheet.getRange(1, YEAR_COL).setValue(parseInt(year));
      wishSheet.getRange(1, MONTH_COL).setValue(parseInt(month));
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
  var ui = SpreadsheetApp.getUi();
  var confirm = ui.alert(
    'AIシフト生成',
    'Claude APIを使ってシフトを自動生成しますか？\n処理に1-2分かかります。',
    ui.ButtonSet.OK_CANCEL
  );
  if (confirm !== ui.Button.OK) return;

  try {
    var ym = getSelectedYearMonth_();
    if (!ym) { ui.alert('年月セレクター(D1/G1)を設定してください'); return; }

    var year = ym.year;
    var month = ym.month;
    var m = ('0' + month).slice(-2);
    var firstDate = year + '-' + m + '-01';
    var lastDay = new Date(parseInt(year), parseInt(month), 0).getDate();
    var lastDate = year + '-' + m + '-' + ('0' + lastDay).slice(-2);

    ui.alert('生成を開始します。完了するまでお待ちください...\n(この画面は閉じてOK)');

    var staff = readStaffMaster_();
    var stores = readStoreMaster_();
    var rules = readRulesMaster_();
    if (!staff.length) throw new Error('スタッフマスタが空です');

    var wishes = readWishesFromSheet_(staff, year);
    var dates = generateDates_(firstDate, lastDate);

    var prompt = buildClaudePrompt_(staff, stores, rules, wishes, dates);
    var response = callClaudeApi_(prompt);
    var result = parseClaudeResponse_(response);

    writeShiftOutput_(result, staff, stores, dates);

    slackPost_(
      '*シフト自動生成完了*\n' +
      '対象期間: ' + firstDate + ' ~ ' + lastDate + '\n' +
      'スタッフ数: ' + staff.length + '名\n' +
      'スプレッドシートの「シフト出力」シートを確認してください。'
    );

    ui.alert(
      'AIシフト生成完了\n\n' +
      '対象期間: ' + firstDate + ' ~ ' + lastDate + '\n' +
      'シフト出力シートを確認してください。'
    );
  } catch (e) {
    slackError_('generateShift', e.message);
    ui.alert('シフト生成エラー: ' + e.message);
  }
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
  var retailStores = stores.filter(function(s) { return s['種別'] === '店舗（営業）'; });
  var otherStores = stores.filter(function(s) { return s['種別'] !== '店舗（営業）'; });

  var staffInfo = staff.map(function(s) {
    return '- ' + s['氏名'] + ' (雇用:' + s['雇用形態'] + ', 働き方:' + s['働き方']
      + ', 役職:' + s['役職'] + ', 週最大:' + (s['最大労働時間/週'] || '40') + 'h'
      + ', 対応店舗:' + (s['対応店舗'] || '全店舗')
      + (s['強制出勤日'] ? ', 強制出勤日:' + s['強制出勤日'] : '')
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
    storeInfo.push('- ' + s['店舗名'] + ': 営業店舗、早番/遅番の2人体制、最低' + (s['最低必要人数/日'] || '2') + '人/日');
  });
  otherStores.forEach(function(s) {
    storeInfo.push('- ' + s['店舗名'] + ': ' + s['種別']);
  });

  var rulesText = rules.length ? rules.map(function(r) { return '- ' + r; }).join('\n') : 'なし';
  var dateLabels = dates.map(function(d) {
    var dt = new Date(d + 'T00:00:00');
    return d + '(' + DOW_JP[dt.getDay()] + ')';
  });
  var staffNames = staff.map(function(s) { return s['氏名']; });
  var retailNames = retailStores.map(function(s) { return s['店舗名']; });

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
    + '## 制約\n'
    + '1. 全スタッフ（固定シフト含む）を配置対象とする\n'
    + '2. 各スタッフの週最大労働時間を超えないこと\n'
    + '3. 希望休（×マーク）は必ず尊重する\n'
    + '4. 対応店舗が指定されているスタッフはその店舗のみに配置\n'
    + '5. 最低週1日の休みを確保\n'
    + '6. 土日祝日も含め全日全店舗カバー\n\n'
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

  return prompt;
}

function callClaudeApi_(prompt) {
  var apiKey = getProp_('ANTHROPIC_API_KEY');
  if (!apiKey) throw new Error('ANTHROPIC_API_KEY が設定されていません');

  var resp = UrlFetchApp.fetch('https://api.anthropic.com/v1/messages', {
    method: 'post',
    contentType: 'application/json',
    headers: {
      'x-api-key': apiKey,
      'anthropic-version': '2023-06-01',
    },
    payload: JSON.stringify({
      model: 'claude-sonnet-4-20250514',
      max_tokens: 8192,
      system: 'あなたはシフト管理の専門家です。与えられた条件を厳密に守り、最適なシフトスケジュールをJSON形式で生成してください。JSONのみを出力し、それ以外のテキストは一切含めないでください。',
      messages: [{ role: 'user', content: prompt }],
    }),
    muteHttpExceptions: true,
  });

  var result = JSON.parse(resp.getContentText());
  if (result.error) throw new Error('Claude API: ' + JSON.stringify(result.error));

  var text = '';
  (result.content || []).forEach(function(block) {
    if (block.type === 'text') text += block.text;
  });
  return text;
}

function parseClaudeResponse_(response) {
  var jsonMatch = response.match(/```(?:json)?\s*\n?([\s\S]*?)\n?```/);
  var jsonStr = jsonMatch ? jsonMatch[1] : response.trim();
  try {
    return JSON.parse(jsonStr);
  } catch (e) {
    throw new Error('Claude応答のJSON解析失敗: ' + e.message + '\n先頭500文字: ' + response.substring(0, 500));
  }
}

// ==========================================================================
// ③ writeShiftOutput_ (横型レイアウト対応)
// ==========================================================================

function writeShiftOutput_(result, staff, stores, dates) {
  var schedule = result.schedule || [];
  var outputSheet = sheet_(SN_OUTPUT);
  if (!outputSheet) throw new Error('シフト出力シートが見つかりません');

  // Clear data area
  outputSheet.getRange(OUTPUT_DSTART, 1, OUTPUT_DAYS * 2, 2).clearContent();
  outputSheet.getRange(OUTPUT_DSTART, 4, OUTPUT_DAYS * 2, OUTPUT_TOTAL_COLS - 3).clearContent();

  // Build schedule lookup
  var dayMap = {};
  schedule.forEach(function(dayData) { dayMap[dayData.date] = dayData; });

  // Read non-retail person headers
  var factoryNames = readNonRetailHeaders_(outputSheet, FACTORY_PERSON_COLS);
  var officeNames = readNonRetailHeaders_(outputSheet, OFFICE_PERSON_COLS);

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

    // Non-retail stores
    ['工場', '本部オフィス'].forEach(function(storeName) {
      var assign = storeAssigns[storeName] || [];
      if (typeof assign === 'object' && !Array.isArray(assign)) {
        assign = Object.values(assign);
      }

      var personCols = storeName === '工場' ? FACTORY_PERSON_COLS : OFFICE_PERSON_COLS;
      var personNames = storeName === '工場' ? factoryNames : officeNames;

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
    });
  });

  Logger.log('Shift output written: ' + dates.length + ' days');
}

// ==========================================================================
// ④ 確定シフト反映 (横型レイアウト対応)
// ==========================================================================

function syncConfirmedShift() {
  var ui = SpreadsheetApp.getUi();
  try {
    var outputSheet = sheet_(SN_OUTPUT);
    if (!outputSheet) { ui.alert('シフト出力シートが見つかりません'); return; }

    var ym = getSelectedYearMonth_();
    if (!ym) { ui.alert('年月セレクター(D1/G1)を設定してください'); return; }
    var year = ym.year;
    var month = ym.month;
    var m = ('0' + month).slice(-2);

    var approver = Session.getActiveUser().getEmail();
    var approvedAt = Utilities.formatDate(new Date(), Session.getScriptTimeZone(), 'yyyy-MM-dd HH:mm:ss');

    var staff = readStaffMaster_();
    var nameToSid = {};
    staff.forEach(function(s) { nameToSid[s['氏名']] = s['staff_id']; });

    // Read non-retail person headers from Row 3
    var factoryNames = readNonRetailHeaders_(outputSheet, FACTORY_PERSON_COLS);
    var officeNames = readNonRetailHeaders_(outputSheet, OFFICE_PERSON_COLS);

    var lastDay = new Date(parseInt(year), parseInt(month), 0).getDate();

    var confirmedDays = [];
    var changedDays = [];
    var confirmedRowNums = [];
    var changedRowNums = [];

    for (var day = 1; day <= lastDay; day++) {
      var nameRowNum = OUTPUT_DSTART + (day - 1) * 2;
      var timeRowNum = nameRowNum + 1;

      var isConfirmed = outputSheet.getRange(nameRowNum, 1).getValue() === true;
      var isChanged = outputSheet.getRange(nameRowNum, 2).getValue() === true;
      if (!isConfirmed && !isChanged) continue;

      var dateStr = year + '-' + m + '-' + ('0' + day).slice(-2);

      // Read retail stores
      for (var storeName in RETAIL_COLS) {
        var shifts = RETAIL_COLS[storeName];
        SHIFT_TYPES.forEach(function(stype) {
          var cols = shifts[stype];
          var staffName = String(outputSheet.getRange(nameRowNum, cols.n).getValue()).trim();
          if (!staffName) return;

          var startTime = String(outputSheet.getRange(timeRowNum, cols.s).getValue()).trim();
          var endTime = String(outputSheet.getRange(timeRowNum, cols.e).getValue()).trim();

          var entry = {
            date: dateStr,
            staff_id: nameToSid[staffName] || '',
            staff_name: staffName,
            store: storeName,
            start_time: startTime,
            end_time: endTime,
            time_range: startTime && endTime ? startTime + '-' + endTime : '',
            shift_type: stype,
            shift_status: '出勤',
          };
          if (isConfirmed) confirmedDays.push(entry);
          if (isChanged) changedDays.push(entry);
        });
      }

      // Read non-retail stores
      function readNonRetail(storeName, personCols, personNames) {
        for (var i = 0; i < personCols.length; i++) {
          var pc = personCols[i];
          var pName = personNames[i];
          if (!pName) continue;

          var attendance = String(outputSheet.getRange(nameRowNum, pc.s).getValue()).trim();
          if (!attendance) continue;

          var shiftStatus = (attendance === '公休') ? '公休' : '出勤';
          var startTime = '';
          var endTime = '';

          if (shiftStatus === '出勤') {
            startTime = String(outputSheet.getRange(timeRowNum, pc.s).getValue()).trim();
            endTime = String(outputSheet.getRange(timeRowNum, pc.e).getValue()).trim();
          }

          var entry = {
            date: dateStr,
            staff_id: nameToSid[pName] || '',
            staff_name: pName,
            store: storeName,
            start_time: startTime,
            end_time: endTime,
            time_range: startTime && endTime ? startTime + '-' + endTime : '',
            shift_type: '',
            shift_status: shiftStatus,
          };
          if (isConfirmed) confirmedDays.push(entry);
          if (isChanged) changedDays.push(entry);
        }
      }

      readNonRetail('工場', FACTORY_PERSON_COLS, factoryNames);
      readNonRetail('本部オフィス', OFFICE_PERSON_COLS, officeNames);

      if (isConfirmed) confirmedRowNums.push(nameRowNum);
      if (isChanged) changedRowNums.push(nameRowNum);
    }

    var totalDays = confirmedDays.concat(changedDays);
    if (!totalDays.length) {
      ui.alert('確定または変更チェックが入った日のデータがありません。');
      return;
    }

    var allDates = totalDays.map(function(d) { return d.date; }).sort();
    var periodStart = allDates[0];
    var periodEnd = allDates[allDates.length - 1];

    var confirmMsg = '確定: ' + confirmedDays.length + '件\n変更: ' + changedDays.length + '件\n\nkintoneとGoogleカレンダーに反映しますか？';
    if (ui.alert('確定シフト反映', confirmMsg, ui.ButtonSet.YES_NO) !== ui.Button.YES) return;

    var kintoneResult = syncToKintone_(totalDays, periodStart, periodEnd, approver, approvedAt, '');
    var calResult = syncToCalendar_(totalDays, year);

    // Uncheck processed rows
    confirmedRowNums.forEach(function(rowNum) { outputSheet.getRange(rowNum, 1).setValue(false); });
    changedRowNums.forEach(function(rowNum) { outputSheet.getRange(rowNum, 2).setValue(false); });

    ui.alert(
      '確定シフト反映完了\n\n' +
      '確定: ' + confirmedDays.length + '件 / 変更: ' + changedDays.length + '件\n' +
      'kintone: 新規 ' + kintoneResult.added + '件 / 更新 ' + kintoneResult.updated + '件\n' +
      'Google Calendar: ' + calResult.created + '件作成 / ' + calResult.deleted + '件削除'
    );

    slackPost_(
      '*確定シフト反映完了*\n' +
      '対象期間: ' + periodStart + ' ~ ' + periodEnd + '\n' +
      '確定: ' + confirmedDays.length + '件 / 変更: ' + changedDays.length + '件'
    );
  } catch (e) {
    slackError_('syncConfirmedShift', e.message);
    ui.alert('エラー: ' + e.message);
  }
}

function syncToKintone_(confirmedDays, periodStart, periodEnd, approver, approvedAt, scheduleVersion) {
  var query = 'period_start = "' + periodStart + '" and period_end = "' + periodEnd
    + '" order by staff_id asc, shift_date asc limit 500';
  var existing = kintoneGet_(KINTONE_CONFIRMED_APP, query);
  var existMap = {};
  (existing.records || []).forEach(function(r) {
    var key = r.staff_id.value + '_' + r.shift_date.value;
    existMap[key] = r['$id'].value;
  });

  var toAdd = [];
  var toUpdate = [];

  confirmedDays.forEach(function(d) {
    var record = {
      staff_id: { value: d.staff_id },
      staff_name: { value: d.staff_name },
      shift_date: { value: d.date },
      shift_status: { value: d.shift_status || '出勤' },
      period_start: { value: periodStart },
      period_end: { value: periodEnd },
      confirmed_by: { value: approver },
      confirmed_at: { value: approvedAt },
    };
    if (scheduleVersion) record.schedule_version = { value: scheduleVersion };
    if (d.store) record.store = { value: d.store };
    if (d.start_time) record.start_time = { value: d.start_time };
    if (d.end_time) record.end_time = { value: d.end_time };

    var key = d.staff_id + '_' + d.date;
    var rid = existMap[key];
    if (rid) {
      toUpdate.push({ id: rid, record: record });
    } else {
      toAdd.push(record);
    }
  });

  var added = 0, updated = 0;
  for (var i = 0; i < toAdd.length; i += 100) {
    kintonePost_(KINTONE_CONFIRMED_APP, toAdd.slice(i, i + 100));
    added += Math.min(100, toAdd.length - i);
  }
  for (var i = 0; i < toUpdate.length; i += 100) {
    kintonePut_(KINTONE_CONFIRMED_APP, toUpdate.slice(i, i + 100));
    updated += Math.min(100, toUpdate.length - i);
  }

  return { added: added, updated: updated };
}

function syncToCalendar_(confirmedDays, year) {
  var calId = getProp_('SHIFT_CALENDAR_ID');
  var cal;
  if (calId) {
    cal = CalendarApp.getCalendarById(calId);
  } else {
    cal = CalendarApp.getDefaultCalendar();
  }
  if (!cal) throw new Error('カレンダーが見つかりません。SHIFT_CALENDAR_IDを確認してください。');

  var byDate = {};
  confirmedDays.forEach(function(d) {
    if (d.shift_status === '公休') return; // 公休はカレンダーに登録しない
    if (!byDate[d.date]) byDate[d.date] = [];
    byDate[d.date].push(d);
  });

  var deleted = 0, created = 0;

  for (var dateStr in byDate) {
    var d = new Date(dateStr + 'T00:00:00');
    var nextDay = new Date(d);
    nextDay.setDate(nextDay.getDate() + 1);

    var existingEvents = cal.getEvents(d, nextDay, { search: '[shift-sync]' });
    existingEvents.forEach(function(ev) {
      if (ev.getTitle().indexOf('[shift-sync]') >= 0) {
        ev.deleteEvent();
        deleted++;
      }
    });

    byDate[dateStr].forEach(function(entry) {
      if (!entry.start_time || !entry.end_time) return;

      var startParts = entry.start_time.split(':');
      var endParts = entry.end_time.split(':');

      var startDate = new Date(d);
      startDate.setHours(parseInt(startParts[0]), parseInt(startParts[1] || '0'), 0, 0);

      var endDate = new Date(d);
      endDate.setHours(parseInt(endParts[0]), parseInt(endParts[1] || '0'), 0, 0);

      if (endDate <= startDate) {
        endDate.setDate(endDate.getDate() + 1);
      }

      var title = '[shift-sync] ' + entry.staff_name
        + (entry.store ? ' @ ' + entry.store : '');

      cal.createEvent(title, startDate, endDate, {
        description: 'シフト: ' + entry.time_range
          + '\n店舗: ' + (entry.store || '未定')
          + '\nstaff_id: ' + entry.staff_id,
      });
      created++;
    });
  }

  return { created: created, deleted: deleted };
}
