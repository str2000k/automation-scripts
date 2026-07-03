#!/usr/bin/env python3
"""Shift schedule validator.

Validates a candidate shift schedule against hard rules:
  1. Minimum staff per day
  2. Maximum consecutive work days
  3. Weekly max work hours
  4. Forced work days
  5. Desired days off respected where possible

Returns a list of violations. Empty list = valid schedule.
"""
from datetime import datetime, timedelta


def _extract_rule_value(rules, pattern, default):
    """Extract a numeric value from free-text rules list using regex pattern.

    Args:
        rules: list of rule strings or dict (legacy format)
        pattern: regex pattern with a capture group for the number
        default: default value if not found
    """
    import re
    # Support legacy dict format
    if isinstance(rules, dict):
        for key in rules:
            if re.search(pattern, key):
                try:
                    return int(rules[key])
                except (ValueError, TypeError):
                    pass
        return default
    # New list format
    for rule in rules:
        m = re.search(pattern, rule)
        if m:
            # Try to find a number near the match
            nums = re.findall(r'\d+', rule)
            if nums:
                return int(nums[0])
    return default


def validate_schedule(schedule, staff, rules, dates, wishes=None, ai_result=None, stores=None):
    """Validate a shift schedule against hard rules.

    Args:
        schedule: dict of {staff_name: [status_per_day]}
                  status is "出勤", "休み", or "希望休"
        staff: list of staff dicts from スタッフマスタ
        rules: list of rule strings (or legacy dict)
        dates: list of date strings (YYYY-MM-DD)
        wishes: list of wish dicts (optional)
        ai_result: AI生成結果のJSON dict（店舗別人員配置を含む、店舗別チェック用）
        stores: 店舗マスタのdict list（最低必要人数/日を含む、店舗別チェック用）

    Returns:
        list of violation dicts: {"rule", "severity", "detail"}
        severity: "hard" (must fix) or "soft" (warning)
    """
    violations = []
    max_consecutive = _extract_rule_value(rules, r"連続", 5)

    # Build staff lookup
    staff_by_name = {s.get("氏名", ""): s for s in staff}

    # Rule 1: 店舗別の最低必要人数チェック（店舗マスタの「最低必要人数/日」を使用）
    # ai_result と stores が両方渡された時のみ実施。渡されない場合は全体合算フォールバック
    if ai_result and stores:
        store_min_map = {}
        for s in stores:
            name = s.get("店舗名", "")
            try:
                min_req = int(float(str(s.get("最低必要人数/日", "0")).strip() or "0"))
            except (ValueError, TypeError):
                min_req = 0
            if name and min_req > 0:
                store_min_map[name] = min_req
        for day in ai_result.get("schedule", []):
            date = day.get("date", "")
            stores_assign = day.get("stores", {})
            for store_name, assignments in stores_assign.items():
                min_req = store_min_map.get(store_name, 0)
                if min_req == 0:
                    continue
                if isinstance(assignments, dict):
                    count = sum(1 for v in assignments.values() if v)
                elif isinstance(assignments, list):
                    count = sum(1 for v in assignments if v)
                else:
                    count = 0
                if count < min_req:
                    violations.append({
                        "rule": "店舗別最低人数不足",
                        "severity": "hard",
                        "detail": f"{date} {store_name}: {count}人 (最低{min_req}人必要)",
                    })
    else:
        # フォールバック: 全体合算の最低人数チェック（共通ルールマスタ "最低*人" 参照、デフォルト3）
        min_staff = _extract_rule_value(rules, r"最低.*人", 3)
        for i, date in enumerate(dates):
            count = 0
            for name, days in schedule.items():
                if i < len(days) and days[i] == "出勤":
                    count += 1
            if count < min_staff:
                violations.append({
                    "rule": "最低必須人数",
                    "severity": "hard",
                    "detail": f"{date}: 出勤{count}人 (最低{min_staff}人必要)",
                })

    # Rule 2: Maximum consecutive work days
    for name, days in schedule.items():
        consecutive = 0
        streak_start = None
        for i, status in enumerate(days):
            if status == "出勤":
                if consecutive == 0:
                    streak_start = dates[i] if i < len(dates) else f"Day{i}"
                consecutive += 1
                if consecutive > max_consecutive:
                    violations.append({
                        "rule": "連続勤務上限",
                        "severity": "hard",
                        "detail": f"{name}: {streak_start}から{consecutive}日連続勤務 (上限{max_consecutive}日)",
                    })
            else:
                consecutive = 0

    # Rule 3: 正社員+固定シフトの月45時間残業上限 (週40時間 = 5日 基本)
    # 役員・アルバイトは対象外（役員は自動生成対象外、アルバイトは希望シフトベース）
    from collections import defaultdict
    for name, days in schedule.items():
        s = staff_by_name.get(name, {})
        if s.get("雇用形態", "") != "正社員":
            continue
        if s.get("働き方", "") != "固定シフト":
            continue
        monthly_overtime = defaultdict(float)
        for week_start in range(0, len(days), 7):
            week_end = min(week_start + 7, len(days))
            week_days = days[week_start:week_end]
            work_count = sum(1 for d in week_days if d == "出勤")
            overtime_hours = max(0, work_count * 8 - 40)
            if overtime_hours > 0 and week_start < len(dates):
                yearmonth = dates[week_start][:7]
                monthly_overtime[yearmonth] += overtime_hours
        for ym, total in monthly_overtime.items():
            if total > 45:
                violations.append({
                    "rule": "月残業上限超過",
                    "severity": "hard",
                    "detail": f"{name}: {ym} 残業{total:.0f}h > 45h",
                })

    # Rule 4: Forced work days
    dow_map = {"月": 0, "火": 1, "水": 2, "木": 3, "金": 4, "土": 5, "日": 6}
    for name, days in schedule.items():
        s = staff_by_name.get(name, {})
        forced = s.get("強制出勤日", "")
        if not forced:
            continue
        forced_dows = set()
        for part in forced.split(","):
            part = part.strip()
            if part in dow_map:
                forced_dows.add(dow_map[part])

        for i, status in enumerate(days):
            if i >= len(dates):
                break
            d = datetime.strptime(dates[i], "%Y-%m-%d")
            if d.weekday() in forced_dows and status != "出勤":
                dow_label = ["月", "火", "水", "木", "金", "土", "日"][d.weekday()]
                violations.append({
                    "rule": "強制出勤日違反",
                    "severity": "hard",
                    "detail": f"{name}: {dates[i]}({dow_label})は強制出勤日だが{status}",
                })

    # Rule 5: Desired days off (soft)
    if wishes:
        wish_map = {}
        for w in wishes:
            wname = w.get("staff_name", "")
            off_dates = set()
            for part in w.get("desired_days_off", "").split(","):
                part = part.strip()
                if part:
                    off_dates.add(part)
            wish_map[wname] = off_dates

        for name, days in schedule.items():
            off_dates = wish_map.get(name, set())
            for i, status in enumerate(days):
                if i >= len(dates):
                    break
                if dates[i] in off_dates and status == "出勤":
                    violations.append({
                        "rule": "希望休未反映",
                        "severity": "soft",
                        "detail": f"{name}: {dates[i]}は希望休だが出勤に割当",
                    })

    return violations


def format_violations(violations):
    """Format violations for display."""
    if not violations:
        return "検証OK: ルール違反なし"

    hard = [v for v in violations if v["severity"] == "hard"]
    soft = [v for v in violations if v["severity"] == "soft"]

    lines = []
    if hard:
        lines.append(f"【ハードルール違反: {len(hard)}件】")
        for v in hard:
            lines.append(f"  [NG] {v['rule']}: {v['detail']}")
    if soft:
        lines.append(f"【ソフトルール警告: {len(soft)}件】")
        for v in soft:
            lines.append(f"  [WARN] {v['rule']}: {v['detail']}")

    return "\n".join(lines)


def has_hard_violations(violations):
    """Check if there are any hard rule violations."""
    return any(v["severity"] == "hard" for v in violations)
