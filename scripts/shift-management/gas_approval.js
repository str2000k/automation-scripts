/**
 * STEP 7: GAS Approval Button for Shift Management
 *
 * Deploy this as a container-bound Apps Script in spreadsheet:
 *   1JlyWngnuha1IHQLMGs5bzTnjct8s7eY4Z-bW0YHIlmU
 *
 * Setup:
 *   1. Open the spreadsheet -> Extensions -> Apps Script
 *   2. Paste this code into Code.gs
 *   3. Run setupButtons() once to create the buttons
 *   4. Set Script Properties (Project Settings -> Script Properties):
 *      - SLACK_BOT_TOKEN
 *      - SLACK_SHIFT_CHANNEL
 *      - NOTIFY_SCRIPT_URL (URL of deployed notify_shift.py webhook, or leave empty)
 *
 * Sheet layout (シフト出力):
 *   A1: "シフト期間"        B1: "2026-03-09 ~ 2026-03-22"
 *   A2: "承認者"            B2: (auto-filled on approval)
 *   A3: "承認日時"          B3: (auto-filled on approval)
 *   A4: "schedule_version"  B4: (auto-filled on approval, e.g. "2026-03-09_v1")
 *   Row 5: (empty)  Row 6: headers  Row 7+: shift data
 */

var SPREADSHEET_ID = '1JlyWngnuha1IHQLMGs5bzTnjct8s7eY4Z-bW0YHIlmU';
var SHEET_NAME = 'シフト出力';

// ---------------------------------------------------------------------------
// Menu & Button Setup
// ---------------------------------------------------------------------------

/**
 * Add custom menu when spreadsheet opens.
 */
function onOpen() {
  var ui = SpreadsheetApp.getUi();
  ui.createMenu('シフト管理')
    .addItem('シフト生成', 'onGenerateShift')
    .addItem('承認する', 'onApproveShift')
    .addSeparator()
    .addItem('ボタン配置 (初回のみ)', 'setupButtons')
    .addToUi();
}

/**
 * Create drawing buttons on the sheet (run once).
 * Note: GAS cannot programmatically create drawings/buttons,
 * so this sets up labels in cells instead. For actual button drawings,
 * manually insert shapes via Insert -> Drawing and assign functions.
 */
function setupButtons() {
  var ss = SpreadsheetApp.getActiveSpreadsheet();
  var sheet = ss.getSheetByName(SHEET_NAME);

  if (!sheet) {
    SpreadsheetApp.getUi().alert('シフト出力シートが見つかりません');
    return;
  }

  // Ensure rows 2-3 have labels
  sheet.getRange('A2').setValue('承認者');
  sheet.getRange('A3').setValue('承認日時');

  // Add instruction in row 4
  sheet.getRange('A4').setValue('');

  // Format label cells
  sheet.getRange('A1:A3').setFontWeight('bold');

  // Create button-like cells in R2 and R3
  var btnGenerate = sheet.getRange('R2');
  btnGenerate.setValue('シフト生成');
  btnGenerate.setBackground('#4285F4');
  btnGenerate.setFontColor('#FFFFFF');
  btnGenerate.setFontWeight('bold');
  btnGenerate.setHorizontalAlignment('center');
  btnGenerate.setNote('メニュー「シフト管理」→「シフト生成」を実行してください');

  var btnApprove = sheet.getRange('R3');
  btnApprove.setValue('承認する');
  btnApprove.setBackground('#34A853');
  btnApprove.setFontColor('#FFFFFF');
  btnApprove.setFontWeight('bold');
  btnApprove.setHorizontalAlignment('center');
  btnApprove.setNote('メニュー「シフト管理」→「承認する」を実行してください');

  SpreadsheetApp.getUi().alert(
    'ボタンを配置しました。\n\n' +
    '図形ボタンを使う場合:\n' +
    '1. 挿入 → 図形描画 で「シフト生成」ボタンを作成\n' +
    '2. 右クリック → スクリプトを割り当て → onGenerateShift\n' +
    '3. 同様に「承認する」ボタン → onApproveShift\n\n' +
    'またはメニュー「シフト管理」からも実行できます。'
  );
}

// ---------------------------------------------------------------------------
// Generate Shift (calls generate_shift.py externally)
// ---------------------------------------------------------------------------

/**
 * Trigger shift generation.
 * This notifies the admin to run generate_shift.py.
 * For full automation, deploy generate_shift.py as a web service
 * and call it via UrlFetchApp.
 */
function onGenerateShift() {
  var ui = SpreadsheetApp.getUi();

  var result = ui.alert(
    'シフト生成',
    'AIシフト自動生成を開始しますか？\n（generate_shift.py を実行します）',
    ui.ButtonSet.OK_CANCEL
  );

  if (result !== ui.Button.OK) return;

  var sheet = SpreadsheetApp.getActiveSpreadsheet().getSheetByName(SHEET_NAME);
  if (!sheet) {
    ui.alert('シフト出力シートが見つかりません');
    return;
  }

  // Clear approval if re-generating
  sheet.getRange('B2').setValue('');
  sheet.getRange('B3').setValue('');

  // Try calling external script if URL is configured
  var props = PropertiesService.getScriptProperties();
  var notifyUrl = props.getProperty('NOTIFY_SCRIPT_URL');

  if (notifyUrl) {
    try {
      var response = UrlFetchApp.fetch(notifyUrl + '/generate', {
        method: 'post',
        contentType: 'application/json',
        payload: JSON.stringify({ action: 'generate' }),
        muteHttpExceptions: true,
      });
      ui.alert('シフト生成をリクエストしました。\nレスポンス: ' + response.getContentText());
    } catch (e) {
      ui.alert('外部スクリプト呼び出しエラー: ' + e.message +
        '\n\nターミナルで手動実行してください:\npython3 generate_shift.py');
    }
  } else {
    ui.alert(
      'シフト生成を開始してください。\n\n' +
      'ターミナルで以下を実行:\n' +
      'cd scripts/shift-management && python3 generate_shift.py'
    );
  }
}

// ---------------------------------------------------------------------------
// Approve Shift
// ---------------------------------------------------------------------------

/**
 * Approve the current shift schedule.
 * - Writes approver email and timestamp to B2:B3
 * - Protects the shift data range
 * - Triggers notification (STEP 8)
 */
function onApproveShift() {
  var ui = SpreadsheetApp.getUi();
  var sheet = SpreadsheetApp.getActiveSpreadsheet().getSheetByName(SHEET_NAME);

  if (!sheet) {
    ui.alert('シフト出力シートが見つかりません');
    return;
  }

  // Check if shift data exists
  var period = sheet.getRange('B1').getValue();
  if (!period) {
    ui.alert('シフトデータがありません。先にシフト生成を実行してください。');
    return;
  }

  // Check if already approved
  var existingApprover = sheet.getRange('B2').getValue();
  if (existingApprover) {
    var reapprove = ui.alert(
      '再承認確認',
      '既に ' + existingApprover + ' により承認済みです。\n再承認しますか？',
      ui.ButtonSet.OK_CANCEL
    );
    if (reapprove !== ui.Button.OK) return;
  }

  // Confirmation dialog
  var confirm = ui.alert(
    'シフト承認',
    '以下のシフトを承認しますか？\n\n' +
    '対象期間: ' + period + '\n\n' +
    '承認すると:\n' +
    '- シフトデータがロックされます\n' +
    '- 全スタッフにSlack/LINEで通知されます\n' +
    '- kintoneに確定シフトが登録されます',
    ui.ButtonSet.OK_CANCEL
  );

  if (confirm !== ui.Button.OK) return;

  // Write approval info
  var approverEmail = Session.getActiveUser().getEmail();
  var approvalTime = Utilities.formatDate(
    new Date(),
    Session.getScriptTimeZone(),
    'yyyy-MM-dd HH:mm:ss'
  );

  // Generate schedule_version
  var periodStart = String(period).split('~')[0].trim(); // "2026-03-09"
  var scheduleVersion = generateScheduleVersion_(periodStart);

  sheet.getRange('B2').setValue(approverEmail);
  sheet.getRange('B3').setValue(approvalTime);
  sheet.getRange('A4').setValue('schedule_version');
  sheet.getRange('B4').setValue(scheduleVersion);

  // Format approval cells
  sheet.getRange('B2:B4').setFontWeight('bold');
  sheet.getRange('B2').setFontColor('#34A853');

  // Protect shift data range
  protectShiftData_(sheet);

  // Trigger STEP 8-11 notifications
  triggerPostApproval_(sheet, period, approverEmail, scheduleVersion);

  ui.alert(
    '承認完了',
    'シフトを承認しました。\n\n' +
    '承認者: ' + approverEmail + '\n' +
    '日時: ' + approvalTime + '\n' +
    'バージョン: ' + scheduleVersion + '\n\n' +
    'ターミナルで後続処理を実行してください:\n' +
    'python3 run_post_approval.py ' + scheduleVersion
  );
}

// ---------------------------------------------------------------------------
// Protection
// ---------------------------------------------------------------------------

/**
 * Protect the shift data range so it cannot be accidentally edited.
 */
function protectShiftData_(sheet) {
  var lastRow = sheet.getLastRow();
  var lastCol = sheet.getLastColumn();

  if (lastRow < 6) return; // No data rows

  // Protect rows 1-lastRow (entire shift output)
  var range = sheet.getRange(1, 1, lastRow, lastCol);
  var protection = range.protect()
    .setDescription('承認済みシフト（' + new Date().toLocaleDateString('ja-JP') + '）');

  // Allow only the approver to edit
  var me = Session.getEffectiveUser();
  protection.addEditor(me);
  try {
    protection.removeEditors(protection.getEditors());
    protection.addEditor(me);
  } catch (e) {
    // Ignore if cannot modify editors (e.g., owner-only)
  }
  protection.setWarningOnly(false);
}

// ---------------------------------------------------------------------------
// Schedule Version
// ---------------------------------------------------------------------------

/**
 * Generate a schedule_version string like "2026-03-09_v1".
 * Checks Script Properties for existing versions of the same period.
 */
function generateScheduleVersion_(periodStart) {
  var props = PropertiesService.getScriptProperties();
  var key = 'version_counter_' + periodStart;
  var counter = parseInt(props.getProperty(key) || '0', 10) + 1;
  props.setProperty(key, String(counter));
  return periodStart + '_v' + counter;
}

// ---------------------------------------------------------------------------
// Post-Approval Trigger (STEP 8-11)
// ---------------------------------------------------------------------------

/**
 * Trigger post-approval processes.
 * Calls notify_shift.py or sends Slack notification directly.
 */
function triggerPostApproval_(sheet, period, approverEmail, scheduleVersion) {
  var props = PropertiesService.getScriptProperties();
  var notifyUrl = props.getProperty('NOTIFY_SCRIPT_URL');

  // Collect shift data for notification
  var shiftData = collectShiftData_(sheet);

  if (notifyUrl) {
    // Call external notification script
    try {
      UrlFetchApp.fetch(notifyUrl + '/notify', {
        method: 'post',
        contentType: 'application/json',
        payload: JSON.stringify({
          action: 'notify',
          period: period,
          approver: approverEmail,
          schedule_version: scheduleVersion,
          shift_data: shiftData,
        }),
        muteHttpExceptions: true,
      });
    } catch (e) {
      notifySlackError_('GAS triggerPostApproval: ' + e.message);
    }
  } else {
    // Direct Slack notification as fallback
    notifySlackApproval_(period, approverEmail, shiftData);
  }
}

/**
 * Collect shift data from the sheet for notifications.
 */
function collectShiftData_(sheet) {
  var lastRow = sheet.getLastRow();
  if (lastRow < 6) return [];

  // Read header (row 5) and data (rows 6+)
  // A5=staff_id, B5=スタッフ名, C5~P5=dates, Q5=出勤日数, R5=合計勤務時間
  var headers = sheet.getRange(5, 3, 1, 14).getValues()[0]; // C5:P5 = dates
  var data = [];

  for (var r = 6; r <= lastRow; r++) {
    var row = sheet.getRange(r, 1, 1, 18).getValues()[0]; // A:R
    var staffId = row[0];
    var name = row[1];
    if (!name) continue;

    var workDays = [];
    var offDays = [];
    var wishOffDays = [];

    for (var d = 0; d < 14; d++) {
      var dateLabel = String(headers[d]).split('\n')[0]; // "2026-03-09"
      var status = row[d + 2]; // C~P columns

      if (status === '出勤') {
        workDays.push(dateLabel);
      } else if (status === '希望休') {
        wishOffDays.push(dateLabel);
      } else {
        offDays.push(dateLabel);
      }
    }

    data.push({
      staff_id: staffId,
      name: name,
      work_days: workDays,
      off_days: offDays,
      wish_off_days: wishOffDays,
      total_work_days: row[16], // Q column
      total_hours: row[17],     // R column
    });
  }

  return data;
}

// ---------------------------------------------------------------------------
// Slack Direct Notification (fallback)
// ---------------------------------------------------------------------------

/**
 * Send approval notification to Slack channel.
 */
function notifySlackApproval_(period, approver, shiftData) {
  var props = PropertiesService.getScriptProperties();
  var token = props.getProperty('SLACK_BOT_TOKEN');
  var channel = props.getProperty('SLACK_SHIFT_CHANNEL') || '#shift-management';

  if (!token) return;

  var summary = shiftData.map(function(s) {
    return s.name + ': 出勤 ' + s.total_work_days + '日 (' + s.total_hours + ')';
  }).join('\n');

  var text =
    '*シフト確定通知*\n\n' +
    '対象期間: *' + period + '*\n' +
    '承認者: ' + approver + '\n' +
    '承認日時: ' + new Date().toLocaleString('ja-JP') + '\n\n' +
    '```\n' + summary + '\n```\n\n' +
    '個別通知は notify_shift.py を実行してください:\n' +
    '`python3 notify_shift.py`';

  postSlackMessage_(token, channel, text);
}

/**
 * Send error notification to Slack.
 */
function notifySlackError_(errorMsg) {
  var props = PropertiesService.getScriptProperties();
  var token = props.getProperty('SLACK_BOT_TOKEN');
  var channel = props.getProperty('SLACK_SHIFT_CHANNEL') || '#shift-management';

  if (!token) return;

  var now = Utilities.formatDate(new Date(), Session.getScriptTimeZone(), 'yyyy-MM-dd HH:mm:ss');
  var text = '【エラー通知】発生箇所:gas_approval エラー:' + errorMsg + ' 日時:' + now;
  postSlackMessage_(token, channel, text);
}

/**
 * Post a message to Slack.
 */
function postSlackMessage_(token, channel, text) {
  try {
    UrlFetchApp.fetch('https://slack.com/api/chat.postMessage', {
      method: 'post',
      contentType: 'application/json',
      headers: { 'Authorization': 'Bearer ' + token },
      payload: JSON.stringify({ channel: channel, text: text }),
      muteHttpExceptions: true,
    });
  } catch (e) {
    Logger.log('Slack post failed: ' + e.message);
  }
}
