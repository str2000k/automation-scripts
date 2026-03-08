#!/usr/bin/env python3
"""Sync outbox for shift post-approval processing.

Manages per-destination delivery status for each schedule_version.
Destinations: slack, line, calendar, kintone, mf_kintai
Statuses: pending, sent, failed, partial_failed

Data is persisted to sync_outbox.json in the same directory.
"""
import json
import os
from datetime import datetime

OUTBOX_FILE = os.path.join(os.path.dirname(__file__), "sync_outbox.json")
DESTINATIONS = ("slack", "line", "calendar", "kintone", "mf_kintai")
VALID_STATUSES = ("pending", "sent", "failed", "partial_failed")


def _load():
    """Load outbox from JSON file."""
    if os.path.exists(OUTBOX_FILE):
        with open(OUTBOX_FILE) as f:
            return json.load(f)
    return {}


def _save(data):
    """Save outbox to JSON file."""
    with open(OUTBOX_FILE, "w") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def create_version(period_start, approver):
    """Create a new schedule_version and initialize outbox entries.

    Args:
        period_start: e.g. "2026-04-01"
        approver: approver email

    Returns:
        schedule_version string, e.g. "2026-04-01_v1"
    """
    data = _load()

    # Find next version number for this period
    version_num = 1
    while f"{period_start}_v{version_num}" in data:
        version_num += 1

    version = f"{period_start}_v{version_num}"
    now = datetime.now().strftime("%Y-%m-%dT%H:%M:%S+09:00")

    data[version] = {
        "created_at": now,
        "approver": approver,
        "period_start": period_start,
        "destinations": {},
    }
    for dest in DESTINATIONS:
        data[version]["destinations"][dest] = {
            "status": "pending",
            "updated_at": now,
            "error": None,
        }

    _save(data)
    return version


def get_status(schedule_version, destination=None):
    """Get outbox status for a schedule_version.

    Args:
        schedule_version: e.g. "2026-04-01_v1"
        destination: optional, one of DESTINATIONS. If None, returns all.

    Returns:
        dict of destination statuses, or single destination status.
    """
    data = _load()
    entry = data.get(schedule_version)
    if not entry:
        return None

    if destination:
        return entry["destinations"].get(destination)
    return entry["destinations"]


def update_status(schedule_version, destination, status, error=None):
    """Update the status of a destination for a schedule_version.

    Args:
        schedule_version: e.g. "2026-04-01_v1"
        destination: one of DESTINATIONS
        status: one of VALID_STATUSES
        error: optional error message string
    """
    if destination not in DESTINATIONS:
        raise ValueError(f"Invalid destination: {destination}. Must be one of {DESTINATIONS}")
    if status not in VALID_STATUSES:
        raise ValueError(f"Invalid status: {status}. Must be one of {VALID_STATUSES}")

    data = _load()
    entry = data.get(schedule_version)
    if not entry:
        raise ValueError(f"Unknown schedule_version: {schedule_version}")

    now = datetime.now().strftime("%Y-%m-%dT%H:%M:%S+09:00")
    entry["destinations"][destination] = {
        "status": status,
        "updated_at": now,
        "error": error,
    }
    _save(data)


def should_run(schedule_version, destination):
    """Check if a destination should be (re-)run.

    Returns True if status is 'pending' or 'failed'.
    """
    status_info = get_status(schedule_version, destination)
    if not status_info:
        return False
    return status_info["status"] in ("pending", "failed")


def get_pending_destinations(schedule_version):
    """Get list of destinations that need processing."""
    statuses = get_status(schedule_version)
    if not statuses:
        return []
    return [d for d in DESTINATIONS if statuses[d]["status"] in ("pending", "failed")]


def get_latest_version():
    """Get the most recently created schedule_version."""
    data = _load()
    if not data:
        return None
    return max(data.keys(), key=lambda v: data[v]["created_at"])


def summary(schedule_version=None):
    """Print summary of outbox status."""
    data = _load()
    if not data:
        print("Outbox is empty.")
        return

    versions = [schedule_version] if schedule_version else sorted(data.keys(), reverse=True)
    for v in versions:
        entry = data.get(v)
        if not entry:
            print(f"  {v}: not found")
            continue
        print(f"\n  {v} (approver: {entry['approver']}, created: {entry['created_at']})")
        for dest, info in entry["destinations"].items():
            status_icon = {"pending": "⏳", "sent": "✅", "failed": "❌", "partial_failed": "⚠️"}.get(info["status"], "?")
            line = f"    {status_icon} {dest}: {info['status']}"
            if info.get("error"):
                line += f" - {info['error']}"
            print(line)
