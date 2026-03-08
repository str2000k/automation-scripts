#!/usr/bin/env python3
"""Run all post-approval steps using sync_outbox.

Executes pending destinations for a schedule_version:
  1. kintone - register confirmed shift
  2. slack - channel announcement + individual DMs
  3. line - individual notifications
  4. calendar - Google Calendar sync
  5. mf_kintai - MF Cloud attendance (not yet implemented)

Usage:
    python3 run_post_approval.py                    # Use latest version
    python3 run_post_approval.py 2026-04-01_v1      # Specify version
    python3 run_post_approval.py --status            # Show outbox status
    python3 run_post_approval.py --retry 2026-04-01_v1  # Retry failed
"""
import sys

import sync_outbox


def run_destination(version, dest):
    """Run a single destination's post-approval process."""
    print(f"\n--- Running: {dest} ---")
    try:
        if dest == "kintone":
            import kintone_shift_register
            kintone_shift_register.run(dry_run=False, schedule_version=version)
        elif dest == "slack":
            import notify_shift
            notify_shift.run(schedule_version=version)
        elif dest == "line":
            # LINE is handled within notify_shift.py
            pass  # already run as part of slack step
        elif dest == "calendar":
            import calendar_sync
            calendar_sync.run(dry_run=False, schedule_version=version)
        elif dest == "mf_kintai":
            print("  [SKIP] MF Cloud attendance sync not yet implemented")
            sync_outbox.update_status(version, "mf_kintai", "pending",
                                      "Not implemented yet")
    except Exception as e:
        print(f"  [ERROR] {dest}: {e}")


def run(version=None):
    """Run all pending post-approval steps."""
    if not version:
        version = sync_outbox.get_latest_version()
    if not version:
        print("No schedule_version found in outbox. Approve a shift first.")
        return

    print(f"=== Post-Approval Processing: {version} ===")
    pending = sync_outbox.get_pending_destinations(version)

    if not pending:
        print("All destinations already processed.")
        sync_outbox.summary(version)
        return

    print(f"Pending destinations: {', '.join(pending)}")

    # Run in order: kintone first (source of truth), then notifications
    order = ["kintone", "slack", "line", "calendar", "mf_kintai"]
    for dest in order:
        if dest in pending:
            run_destination(version, dest)

    print("\n=== Post-Approval Summary ===")
    sync_outbox.summary(version)


def main():
    if "--status" in sys.argv:
        version = None
        for arg in sys.argv[1:]:
            if not arg.startswith("-"):
                version = arg
        sync_outbox.summary(version)
        return

    if "--retry" in sys.argv:
        idx = sys.argv.index("--retry")
        version = sys.argv[idx + 1] if idx + 1 < len(sys.argv) else None
        if not version:
            print("Usage: --retry <schedule_version>")
            return
        run(version)
        return

    version = None
    for arg in sys.argv[1:]:
        if not arg.startswith("-"):
            version = arg
    run(version)


if __name__ == "__main__":
    main()
