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


def validate_schedule(schedule, staff, rules, dates, wishes=None):
    """Validate a shift schedule against hard rules.

    Args:
        schedule: dict of {staff_name: [status_per_day]}
                  status is "出勤", "休み", or "希望休"
        staff: list of staff dicts from スタッフマスタ
        rules: dict from ルールマスタ
        dates: list of date strings (YYYY-MM-DD)
        wishes: list of wish dicts (optional)

    Returns:
        list of violation dicts: {"rule", "severity", "detail"}
        severity: "hard" (must fix) or "soft" (warning)
    """
    violations = []
    min_staff = int(rules.get("最低必須人数", "3"))
    max_consecutive = int(rules.get("連続勤務上限日数", "5"))

    # Build staff lookup
    staff_by_name = {s.get("氏名", ""): s for s in staff}

    # Rule 1: Minimum staff per day
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

    # Rule 3: Weekly max work hours (8h/day)
    for name, days in schedule.items():
        s = staff_by_name.get(name, {})
        max_hours = float(s.get("最大労働時間/週", "40"))
        max_days_per_week = max_hours / 8

        # Check each 7-day window
        for week_start in range(0, len(days), 7):
            week_end = min(week_start + 7, len(days))
            week_days = days[week_start:week_end]
            work_count = sum(1 for d in week_days if d == "出勤")
            if work_count > max_days_per_week:
                week_label = dates[week_start] if week_start < len(dates) else f"Week{week_start // 7 + 1}"
                violations.append({
                    "rule": "週最大労働時間超過",
                    "severity": "hard",
                    "detail": f"{name}: {week_label}週 出勤{work_count}日 ({work_count * 8}h > {max_hours}h)",
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
