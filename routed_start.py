#!/usr/bin/env python3
from __future__ import annotations

import os
import sys
from pathlib import Path


def main() -> int:
    from omnigent.codex_account_pool import CodexAccountPool

    pool = CodexAccountPool.from_default()
    if not pool.enabled:
        print("Subscription routing is disabled or unconfigured. Run omni-rotate accounts.", file=sys.stderr)
        return 1
    with pool._locked_state() as state:
        now = int(pool._now())
        pool._prune(state, now)
        profile = pool._choose(state, now)
    if profile is None:
        print("No subscription is available. Add an account or wait for its quota to reset.", file=sys.stderr)
        return 1
    executable = Path(sys.executable).with_name("omni")
    os.execv(str(executable), [str(executable), profile.provider, *sys.argv[1:]])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
