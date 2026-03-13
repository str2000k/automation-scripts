/**
 * STEP 7: GAS Approval System for Shift Management (3-Button)
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
 *   A5: "承認ステータス"    B5: (承認済 / 一時保留 / 差し戻し: reason)
 *   Row 6: headers
 *   Row 7+: shift data
 */

var SPREADSHEET_ID = '1JlyWngnuha1IHQLMGs5bzTnjct8s7eY4Z-bW0YHIlmU';
var SHEET_NAME = 'シフト出力';

// Config via Script Properties (set by initializeSystem or manually)
var DEFAULT_SLACK_SHIFT_CHANNEL = 'C0AKBJ1LTV2';

function _getSlackToken() {
  var props = PropertiesService.getScriptProperties();
  return props.getProperty('SLACK_BOT_TOKEN') || '';
}

function _getSlackChannel() {
  var props = PropertiesService.getScriptProperties();
  return props.getProperty('SLACK_SHIFT_CHANNEL') || DEFAULT_SLACK_SHIFT_CHANNEL;
}

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
    .addSeparator()
    .addItem('承認する', 'approveShift')
    .addItem('一時保留', 'holdShift')
    .addItem('差し戻す', 'rejectShift')
    .addSeparator()
    .addItem('ボタン配置 (初回のみ)', 'setupButtons')
    .addToUi();
}

/**
 * Create button-like cells on the sheet (run once).
 * For actual button drawings, manually insert shapes via Insert -> Drawing
 * and assign the corresponding function names.
 */
function setupButtons() {
  var ss = SpreadsheetApp.getActiveSpreadsheet();
  var sheet = ss.getSheetByName(SHEET_NAME);

  if (!sheet) {
    SpreadsheetApp.getUi().alert('シフト出力シートが見つかりません');
    return;
  }

  // Ensure metadata rows have labels
  sheet.getRange('A1').setValue('シフト期間');
  sheet.getRange('A2').setValue('承認者');
  sheet.getRange('A3').setValue('承認日時');
  sheet.getRange('A4').setValue('schedule_version');
  sheet.getRange('A5').setValue('承認ステータス');

  // Format label cells
  sheet.getRange('A1:A5').setFontWeight('bold');

  // --- Button-like cells in column R ---

  // R2: 承認する (green)
  var btnApprove = sheet.getRange('R2');
  btnApprove.setValue('承認する');
  btnApprove.setBackground('#34A853');
  btnApprove.setFontColor('#FFFFFF');
  btnApprove.setFontWeight('bold');
  btnApprove.setHorizontalAlignment('center');
  btnApprove.setNote('メニュー「シフト管理」→「承認する」を実行、または図形ボタンに approveShift を割り当て');

  // R3: 一時保留 (yellow)
  var btnHold = sheet.getRange('R3');
  btnHold.setValue('一時保留');
  btnHold.setBackground('#F4B400');
  btnHold.setFontColor('#FFFFFF');
  btnHold.setFontWeight('bold');
  btnHold.setHorizontalAlignment('center');
  btnHold.setNote('メニュー「シフト管理」→「一時保留」を実行、または図形ボタンに holdShift を割り当て');

  // R4: 差し戻す (red)
  var btnReject = sheet.getRange('R4');
  btnReject.setValue('差し戻す');
  btnReject.setBackground('#DB4437');
  btnReject.setFontColor('#FFFFFF');
  btnReject.setFontWeight('bold');
  btnReject.setHorizontalAlignment('center');
  btnReject.setNote('メニュー「シフト管理」→「差し戻す」を実行、または図形ボタンに rejectShift を割り当て');

  SpreadsheetApp.getUi().alert(
    'ボタンを配置しました。\n\n' +
    'R2: 承認する (緑)\n' +
    'R3: 一時保留 (黄)\n' +
    'R4: 差し戻す (赤)\n\n' +
    '図形ボタンを使う場合:\n' +
    '1. 挿入 → 図形描画 でボタンを作成\n' +
    '2. 右クリック → スクリプトを割り当て:\n' +
    '   - 承認 → approveShift\n' +
    '   - 保留 → holdShift\n' +
    '   - 差し戻し → rejectShift\n\n' +
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

  // Clear approval fields if re-generating
  sheet.getRange('B2').setValue('');
  sheet.getRange('B3').setValue('');
  sheet.getRange('B4').setValue('');
  sheet.getRange('B5').setValue('');

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
 * - Writes approver email, timestamp, schedule_version to B2:B4
 * - Writes 承認ステータス = 承認済 to A5:B5
 * - Protects the shift data range
 * - Triggers notification (STEP 8)
 */
function approveShift() {
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
      ui.ButtonSet.YES_NO
    );
    if (reapprove !== ui.Button.YES) return;
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
    ui.ButtonSet.YES_NO
  );

  if (confirm !== ui.Button.YES) return;

  // Write approval info
  var approverEmail = Session.getActiveUser().getEmail();
  var approvalTime = Utilities.formatDate(
    new Date(),
    Session.getScriptTimeZone(),
    'yyyy-MM-dd HH:mm:ss'
  );

  // Generate schedule_version
  var periodStart = String(period).split('~')[0].trim();
  var scheduleVersion = generateScheduleVersion_(periodStart);

  sheet.getRange('A2').setValue('承認者');
  sheet.getRange('B2').setValue(approverEmail);
  sheet.getRange('A3').setValue('承認日時');
  sheet.getRange('B3').setValue(approvalTime);
  sheet.getRange('A4').setValue('schedule_version');
  sheet.getRange('B4').setValue(scheduleVersion);
  sheet.getRange('A5').setValue('承認ステータス');
  sheet.getRange('B5').setValue('承認済');

  // Format approval cells
  sheet.getRange('B2:B4').setFontWeight('bold');
  sheet.getRange('B2').setFontColor('#34A853');
  sheet.getRange('B5').setFontWeight('bold').setFontColor('#34A853');

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
// Hold Shift
// ---------------------------------------------------------------------------

/**
 * Put the shift on hold (一時保留).
 * - Writes 承認ステータス = 一時保留 to A5:B5
 * - Does NOT protect the sheet (allows further editing)
 */
function holdShift() {
  var ui = SpreadsheetApp.getUi();
  var sheet = SpreadsheetApp.getActiveSpreadsheet().getSheetByName(SHEET_NAME);

  if (!sheet) {
    ui.alert('シフト出力シートが見つかりません');
    return;
  }

  var period = sheet.getRange('B1').getValue();
  if (!period) {
    ui.alert('シフトデータがありません。先にシフト生成を実行してください。');
    return;
  }

  sheet.getRange('A5').setValue('承認ステータス');
  sheet.getRange('B5').setValue('一時保留');
  sheet.getRange('B5').setFontWeight('bold').setFontColor('#F4B400');

  ui.alert('一時保留にしました。修正後に再度承認してください。');
}

// ---------------------------------------------------------------------------
// Reject Shift
// ---------------------------------------------------------------------------

/**
 * Reject the shift (差し戻し).
 * - Prompts for rejection reason
 * - Writes 承認ステータス = 差し戻し: <reason> to A5:B5
 * - Notifies Slack #shift-management
 */
function rejectShift() {
  var ui = SpreadsheetApp.getUi();
  var sheet = SpreadsheetApp.getActiveSpreadsheet().getSheetByName(SHEET_NAME);

  if (!sheet) {
    ui.alert('シフト出力シートが見つかりません');
    return;
  }

  var period = sheet.getRange('B1').getValue();
  if (!period) {
    ui.alert('シフトデータがありません。先にシフト生成を実行してください。');
    return;
  }

  // Prompt for rejection reason
  var response = ui.prompt(
    'シフト差し戻し',
    '差し戻し理由を入力してください:',
    ui.ButtonSet.OK_CANCEL
  );

  if (response.getSelectedButton() !== ui.Button.OK) return;

  var reason = response.getResponseText().trim();
  if (!reason) {
    ui.alert('差し戻し理由を入力してください。');
    return;
  }

  var rejector = Session.getActiveUser().getEmail();

  sheet.getRange('A5').setValue('承認ステータス');
  sheet.getRange('B5').setValue('差し戻し: ' + reason);
  sheet.getRange('B5').setFontWeight('bold').setFontColor('#DB4437');

  // Clear any previous approval info
  sheet.getRange('B2').setValue('');
  sheet.getRange('B3').setValue('');
  sheet.getRange('B4').setValue('');

  // Notify Slack
  notifyRejection_(period, rejector, reason);

  ui.alert('差し戻しました。Slackに通知しました。');
}

// ---------------------------------------------------------------------------
// Rejection Slack Notification
// ---------------------------------------------------------------------------

/**
 * Post a Block Kit message to Slack about the shift rejection.
 */
function notifyRejection_(period, rejector, reason) {
  var token = _getSlackToken();
  var channel = _getSlackChannel();

  if (!token) return;

  var now = Utilities.formatDate(new Date(), Session.getScriptTimeZone(), 'yyyy-MM-dd HH:mm:ss');

  var blocks = [
    {
      type: 'header',
      text: {
        type: 'plain_text',
        text: 'シフト差し戻し通知',
        emoji: true,
      },
    },
    {
      type: 'section',
      fields: [
        { type: 'mrkdwn', text: '*対象期間:*\n' + period },
        { type: 'mrkdwn', text: '*差し戻し者:*\n' + rejector },
      ],
    },
    {
      type: 'section',
      text: {
        type: 'mrkdwn',
        text: '*差し戻し理由:*\n' + reason,
      },
    },
    {
      type: 'context',
      elements: [
        { type: 'mrkdwn', text: now + ' | シフト管理システム' },
      ],
    },
  ];

  var fallbackText = 'シフト差し戻し: ' + period + ' 理由: ' + reason + ' (' + rejector + ')';

  try {
    UrlFetchApp.fetch('https://slack.com/api/chat.postMessage', {
      method: 'post',
      contentType: 'application/json',
      headers: { 'Authorization': 'Bearer ' + token },
      payload: JSON.stringify({
        channel: channel,
        text: fallbackText,
        blocks: blocks,
      }),
      muteHttpExceptions: true,
    });
  } catch (e) {
    Logger.log('Slack rejection notification failed: ' + e.message);
  }
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

  if (lastRow < 7) return; // No data rows (headers at row 6, data at row 7+)

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
 * Layout: Row 6 = headers, Row 7+ = data
 */
function collectShiftData_(sheet) {
  var lastRow = sheet.getLastRow();
  if (lastRow < 7) return [];

  // Read header (row 6) and data (rows 7+)
  // A6=staff_id, B6=スタッフ名, C6~P6=dates, Q6=出勤日数, R6=合計勤務時間
  var headers = sheet.getRange(6, 3, 1, 14).getValues()[0]; // C6:P6 = dates
  var data = [];

  for (var r = 7; r <= lastRow; r++) {
    var row = sheet.getRange(r, 1, 1, 18).getValues()[0]; // A:R
    var staffId = row[0];
    var name = row[1];
    if (!name) continue;

    var workDays = [];
    var offDays = [];
    var wishOffDays = [];

    for (var d = 0; d < 14; d++) {
      var dateLabel = String(headers[d]).split('\n')[0];
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
  var token = _getSlackToken();
  var channel = _getSlackChannel();

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
  var token = _getSlackToken();
  var channel = _getSlackChannel();

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
