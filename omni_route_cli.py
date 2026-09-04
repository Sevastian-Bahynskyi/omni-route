#!/usr/bin/env python3
"""Omni Route native command line.

    omni-route native start [workspace]   launch the selected account and supervise
    omni-route native status              print routing status as JSON
    omni-route native rotate              rotate now, manually
    omni-route native threshold [percent] show or set the switch threshold
    omni-route native usage               read quota for every account
    omni-route native doctor              check the native path end to end
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import desktop
import native_scheduler as ns
import quota
import supervisor
from account_pool import (
    MAX_ROTATE_AT_PERCENT,
    AccountPool,
    PoolConfig,
    clamp_threshold,
    preparation_percent,
)

CONFIG_PATH = Path.home() / ".omnigent" / "codex-account-pool.json"
POLL_SECONDS = 90


def _pool() -> AccountPool:
    return AccountPool(PoolConfig.load())


def cmd_status(args: argparse.Namespace) -> int:
    sup = supervisor.Supervisor(Path(args.workspace).resolve())
    print(json.dumps(sup.status(), indent=2))
    return 0


def cmd_usage(args: argparse.Namespace) -> int:
    pool = _pool()
    if not pool.enabled:
        print("pool is disabled or unconfigured", file=sys.stderr)
        return 1
    threshold = pool.config.rotate_at_percent
    print(f"switch at {threshold:g}%  preparation at {preparation_percent(threshold):g}%\n")
    for profile in pool.config.accounts:
        if args.account and profile.name != args.account:
            continue
        usage = quota.read_usage(profile)
        if not usage.known:
            print(f"  {profile.name:<12} {profile.provider:<7} unknown ({usage.detail or 'no reading'})")
            continue
        decisive = usage.decisive_percent or 0.0
        phase = supervisor.evaluate(usage, rotate_at_percent=threshold)
        print(
            f"  {profile.name:<12} {profile.provider:<7} "
            f"5h {usage.session.used_percent or 0:>5.1f}%  "
            f"week {usage.weekly.used_percent or 0:>5.1f}%  "
            f"-> {decisive:>5.1f}%  {phase.value}"
        )
    return 0


def cmd_threshold(args: argparse.Namespace) -> int:
    config = json.loads(CONFIG_PATH.read_text(encoding="utf-8")) if CONFIG_PATH.exists() else {}
    if args.percent is None:
        current = clamp_threshold(float(config.get("rotate_at_percent", 90)))
        print(f"switch      {current:g}%")
        print(f"preparation {preparation_percent(current):g}%")
        print(f"maximum     {MAX_ROTATE_AT_PERCENT:g}%")
        return 0
    requested = float(args.percent)
    applied = clamp_threshold(requested)
    if applied != requested:
        print(f"{requested:g}% exceeds the maximum; using {applied:g}%")
    config["rotate_at_percent"] = applied
    temporary = CONFIG_PATH.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(config, indent=2), encoding="utf-8")
    temporary.replace(CONFIG_PATH)
    print(f"switch      {applied:g}%")
    print(f"preparation {preparation_percent(applied):g}%")
    return 0


def cmd_rotate(args: argparse.Namespace) -> int:
    sup = supervisor.Supervisor(Path(args.workspace).resolve())
    pool = sup.pool
    if not pool.enabled:
        print("pool is disabled or unconfigured", file=sys.stderr)
        return 1
    current_name = pool.snapshot().get("current_account")
    current = next((a for a in pool.config.accounts if a.name == current_name), None)
    result = sup.rotate(reason="manual rotation", exhausted=current)
    print(json.dumps(result.to_dict(), indent=2))
    return 0 if result.outcome is supervisor.Outcome.OK else 1


def cmd_start(args: argparse.Namespace) -> int:
    workspace = Path(args.workspace).resolve()
    sup = supervisor.Supervisor(workspace)
    pool = sup.pool
    if not pool.enabled:
        print("pool is disabled or unconfigured. Run: omni-rotate accounts", file=sys.stderr)
        return 1

    profile = pool.account_for_session(str(workspace))
    if profile is None:
        print("no account is available; all are exhausted or unauthenticated", file=sys.stderr)
        return 1
    print(f"account   {profile.name} ({profile.provider})")
    print(f"workspace {workspace}")

    try:
        sup.prepare_account(profile)
    except Exception as exc:  # noqa: BLE001 - report and stop, never continue blind
        print(f"could not prepare {profile.name}: {exc}", file=sys.stderr)
        return 1

    ok, detail = sup.verify_account(profile)
    if not ok:
        print(f"identity check failed: {detail}", file=sys.stderr)
        return 1
    print(f"identity  {detail}")

    if not args.no_launch:
        try:
            sup.start_provider(profile)
            print("launched  native app")
        except desktop.DesktopError as exc:
            print(f"launch failed: {exc}", file=sys.stderr)
            return 1

    if args.once:
        phase, usage = sup.poll_once(profile)
        print(f"phase     {phase.value}  ({usage.decisive_percent}%)")
        return 0

    print(f"supervising; polling every {POLL_SECONDS}s. Ctrl+C to stop.")
    try:
        while True:
            phase, usage = sup.poll_once(profile)
            stamp = time.strftime("%H:%M:%S")
            percent = usage.decisive_percent
            reading = f"{percent:.1f}%" if percent is not None else "unknown"
            print(f"  {stamp}  {profile.name}  {reading}  {phase.value}")
            if phase is supervisor.Phase.SWITCH:
                print("  threshold reached; rotating")
                result = sup.rotate(exhausted=profile)
                print(json.dumps(result.to_dict(), indent=2))
                if result.outcome in {supervisor.Outcome.NEEDS_USER_ACTION,
                                      supervisor.Outcome.FAILED}:
                    return 1
                nxt = next((a for a in pool.config.accounts
                            if a.name == result.to_account), None)
                if nxt is None:
                    return 1
                profile = nxt
            time.sleep(POLL_SECONDS)
    except KeyboardInterrupt:
        print("\nstopped")
        return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    workspace = Path(args.workspace).resolve()
    pool = _pool()
    problems: list[str] = []

    def check(label: str, ok: bool, detail: str = "") -> None:
        mark = "ok  " if ok else "FAIL"
        print(f"  [{mark}] {label}{(' - ' + detail) if detail else ''}")
        if not ok:
            problems.append(label)

    print("Omni Route native path\n")
    check("pool configured", pool.enabled)
    check("Claude Desktop installed", desktop.CLAUDE_APP.is_dir(), str(desktop.CLAUDE_APP))
    check("Codex Desktop installed", desktop.CODEX_APP.is_dir(), str(desktop.CODEX_APP))

    threshold = pool.config.rotate_at_percent
    check("threshold within cap", threshold <= MAX_ROTATE_AT_PERCENT,
          f"{threshold:g}% (max {MAX_ROTATE_AT_PERCENT:g}%)")
    check("preparation derived", pool.config.preparation_at_percent == threshold - 3,
          f"{pool.config.preparation_at_percent:g}%")

    for profile in pool.config.accounts:
        if profile.provider == "codex":
            identity = desktop.codex_identity(profile.auth_json) if profile.auth_json else None
            check(f"{profile.name} credential", bool(identity and identity.known),
                  (identity.account if identity and identity.known else identity.detail if identity else "missing") or "")
        else:
            user_data = desktop.claude_user_data_dir(profile)
            if not user_data.is_dir():
                check(f"{profile.name} desktop profile", False,
                      f"not signed in yet: run omni-route native signin {profile.name}")
                continue
            ok, detail = desktop.verify_claude_account(user_data, profile.config_dir or Path())
            check(f"{profile.name} desktop profile", ok, detail)

    print()
    if problems:
        print(f"{len(problems)} problem(s): " + ", ".join(problems))
        return 1
    print("native path is ready")
    return 0


def cmd_signin(args: argparse.Namespace) -> int:
    """Open an account's desktop profile so it can be signed in once."""
    pool = _pool()
    profile = next((a for a in pool.config.accounts if a.name == args.account), None)
    if profile is None or profile.provider != "claude":
        print(f"{args.account} is not a configured Claude account", file=sys.stderr)
        return 1
    user_data = desktop.launch_claude(profile, profile.config_dir)
    print(f"opened {profile.name} at {user_data}")
    print("Sign in in that window. Each account keeps its own profile, so a new")
    print("profile always starts signed out; that is expected.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="omni-route native", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    def add(name: str, handler, **kwargs):
        p = sub.add_parser(name, **kwargs)
        p.add_argument("--workspace", default=".", help="workspace directory")
        p.set_defaults(handler=handler)
        return p

    start = add("start", cmd_start, help="launch the selected account and supervise")
    start.add_argument("--once", action="store_true", help="poll once and exit")
    start.add_argument("--no-launch", action="store_true", help="do not start the app")
    add("status", cmd_status, help="print routing status")
    add("rotate", cmd_rotate, help="rotate accounts now")
    add("doctor", cmd_doctor, help="check the native path")
    usage = add("usage", cmd_usage, help="read quota for every account")
    usage.add_argument("--account", help="only this account")
    threshold = add("threshold", cmd_threshold, help="show or set the switch threshold")
    threshold.add_argument("percent", nargs="?", type=float)
    signin = add("signin", cmd_signin, help="open an account profile to sign in")
    signin.add_argument("account")

    args = parser.parse_args(argv)
    return args.handler(args)


if __name__ == "__main__":
    raise SystemExit(main())
