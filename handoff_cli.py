#!/usr/bin/env python3
"""Command line front end to handoff.py, for use from inside an agent turn."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import handoff


def main() -> int:
    parser = argparse.ArgumentParser(description="Read or write a rotation handoff")
    sub = parser.add_subparsers(dest="command", required=True)

    write = sub.add_parser("write", help="write a handoff and arm it")
    write.add_argument("--workspace", default=".")
    for name in ("goal", "progress", "decisions", "files", "status", "blockers"):
        write.add_argument(f"--{name}", default="")
    write.add_argument("--next-action", dest="next_action", default="")
    write.add_argument("--from-account", dest="from_account", default="")
    write.add_argument("--to-account", dest="to_account", default="")

    show = sub.add_parser("show", help="print the latest handoff as JSON")
    show.add_argument("--workspace", default=".")

    pending = sub.add_parser("pending", help="exit 0 if a handoff is armed")
    pending.add_argument("--workspace", default=".")

    args = parser.parse_args()
    workspace = Path(args.workspace).resolve()

    if args.command == "write":
        record = handoff.Handoff(
            goal=args.goal,
            progress=args.progress,
            decisions=args.decisions,
            files=args.files,
            status=args.status,
            blockers=args.blockers,
            next_action=args.next_action,
            from_account=args.from_account,
            to_account=args.to_account,
        )
        path = handoff.write(workspace, record)
        print(f"handoff written: {path}")
        return 0

    if args.command == "show":
        record = handoff.read_latest(workspace)
        if record is None:
            print("no handoff")
            return 1
        print(json.dumps(record.__dict__, indent=2))
        return 0

    return 0 if handoff.is_pending(workspace) else 1


if __name__ == "__main__":
    raise SystemExit(main())
