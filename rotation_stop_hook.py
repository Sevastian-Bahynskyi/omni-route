#!/usr/bin/env python3
"""Stop hook that performs a rotation wrap-up at a real turn boundary.

Gate 4 established the mechanism: a Stop hook that exits with code 2 and writes
to stderr makes the agent act on that text before stopping.

Two rules govern this file:

* **Guarded.** Exit 2 prevents stopping, so an unguarded hook loops forever.
  Each rotation generation may fire exactly once; the acknowledgement file is
  what stops the loop.
* **Fail open.** Any unexpected condition exits 0. A broken hook must never trap
  a session in a loop, and never block a user who is not rotating at all.

Install as a Stop hook:

    {"hooks": {"Stop": [{"hooks": [
        {"type": "command", "command": "<path>/rotation_stop_hook.py"}]}]}}
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

REQUEST = "rotation-request.json"
DIRNAME = ".omni-route"

PREPARE = """Your subscription quota for this account is close to its rotation
threshold, so wind down now rather than starting anything long.

Finish only the unit of work you are in the middle of. Do not begin new
multi-step work, long refactors, or anything you cannot complete shortly. If the
current unit is already complete, simply stop.

Do not mention this message to the user or ask them anything about it."""

SWITCH = """Your subscription quota for this account has reached the rotation
threshold. Work is about to continue on a different account, so hand over now.

Do these steps, in order, then stop:

1. Commit your work in progress with git (commit only, do not push). If there is
   nothing to commit, skip this step.
2. Write a handoff by running:
     python3 {tool} write --workspace {workspace} \\
       --goal '<the task>' --progress '<what is done>' \\
       --decisions '<important decisions>' --files '<changed/relevant files>' \\
       --status '<test and build status>' --blockers '<blockers, or none>' \\
       --next-action '<the exact next action to take>'
   Keep each field short and concrete. The repository is the source of truth;
   the handoff is only a pointer.
3. Stop. Do not start any new work.

Do not ask the user anything. They should not have to notice the account
changed."""


def _workspace() -> Path:
    for key in ("CLAUDE_PROJECT_DIR", "PWD"):
        value = os.environ.get(key)
        if value and Path(value).is_dir():
            return Path(value)
    return Path.cwd()


def main() -> int:
    # Always drain stdin so the caller never blocks on a full pipe.
    try:
        sys.stdin.read()
    except Exception:
        pass

    try:
        workspace = _workspace()
        request_path = workspace / DIRNAME / REQUEST
        if not request_path.exists():
            return 0  # no rotation armed: the common case

        request = json.loads(request_path.read_text(encoding="utf-8"))
        if not isinstance(request, dict):
            return 0
        phase = request.get("phase")
        if phase not in {"prepare", "switch"}:
            return 0

        generation = str(request.get("generation", "0"))
        ack = workspace / DIRNAME / f"rotation-ack-{phase}-{generation}"
        if ack.exists():
            return 0  # already fired for this generation: let the turn end

        ack.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        ack.write_text("", encoding="utf-8")

        if phase == "prepare":
            message = PREPARE
        else:
            message = SWITCH.format(
                tool=Path(__file__).with_name("handoff_cli.py"),
                workspace=workspace,
            )
        print(message, file=sys.stderr)
        return 2
    except Exception:
        # Fail open. Never trap the session because this hook misbehaved.
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
