#!/usr/bin/env python3
"""Verify a per-account Claude Desktop profile is usable by Omni Route.

    python3 verify_profile.py <user-data-dir> [claude-config-dir]

Read-only. Reports the account UUID, the scheduled-task store, and whether the
rotation automation could be installed. Never writes to the app's state: the
install is a dry run, because the app caches its task store in memory and must
be stopped before a real write.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import native_scheduler as ns


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    user_data_dir = Path(sys.argv[1]).expanduser()
    config_dir = Path(sys.argv[2]).expanduser() if len(sys.argv) > 2 else None
    repo = Path(__file__).resolve().parent

    print(f"user-data-dir : {user_data_dir}")
    print(f"exists        : {user_data_dir.is_dir()}")
    if config_dir:
        print(f"config-dir    : {config_dir} (exists: {config_dir.is_dir()})")

    if not user_data_dir.is_dir():
        print("\nFAIL: that profile directory does not exist.")
        return 1

    try:
        store = ns.find_store(user_data_dir)
    except ns.SchedulerError as exc:
        print(f"\nFAIL: {exc}")
        print("\nCreate any one scheduled task in this profile's Code tab, then re-run.")
        return 1

    print(f"account uuid  : {store.account_uuid}")
    print(f"task store    : {store.path}")

    try:
        data = ns.load_store(store)
    except ns.SchedulerError as exc:
        print(f"\nFAIL: task store did not validate: {exc}")
        return 1

    tasks = [t.get("id") for t in data["scheduledTasks"]]
    print(f"existing tasks: {tasks}")

    result = ns.install(
        user_data_dir, repo, claude_config_dir=config_dir, dry_run=True
    )
    print(f"\nrotation task : {result['action']}")
    print(json.dumps(result["record"], indent=2))
    print("\nPASS: this profile is usable. Nothing was written.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
