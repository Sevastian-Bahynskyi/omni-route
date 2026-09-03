#!/usr/bin/env python3
"""Interactive subscription configurator for the Omnigent rotation extension."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

CONFIG = Path("~/.omnigent/codex-account-pool.json").expanduser()
STATE = Path("~/.omnigent/codex-account-pool-state.json").expanduser()
ROOT = Path("~/.omnigent/codex-accounts").expanduser()
CLAUDE_AGENT = "claude-native-ui"


def _load() -> tuple[list[dict[str, str]], bool]:
    if not CONFIG.exists():
        return [], False
    try:
        raw: Any = json.loads(CONFIG.read_text(encoding="utf-8"))
    except Exception:
        return [], False
    if not isinstance(raw, dict):
        return [], False
    accounts: list[dict[str, str]] = []
    for value in raw.get("accounts", []):
        if not isinstance(value, dict):
            continue
        name = value.get("name")
        auth_json = value.get("auth_json")
        if isinstance(name, str) and name.strip() and isinstance(auth_json, str) and auth_json.strip():
            accounts.append({"name": name.strip(), "auth_json": auth_json.strip()})
    fallback = raw.get("claude_fallback_agent")
    return accounts, isinstance(fallback, str) and bool(fallback.strip())


def _save(accounts: list[dict[str, str]], claude_enabled: bool) -> None:
    CONFIG.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    payload = {
        "enabled": bool(accounts),
        "rotate_at_percent": 99,
        "claude_fallback_agent": CLAUDE_AGENT if claude_enabled else None,
        "accounts": accounts,
    }
    tmp = CONFIG.with_suffix(CONFIG.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, CONFIG)
    for path in (STATE, Path(str(STATE) + ".lock")):
        try:
            path.unlink()
        except FileNotFoundError:
            pass


def _next_codex_name(accounts: list[dict[str, str]]) -> str:
    used = {a["name"] for a in accounts}
    number = 1
    while f"codex-{number}" in used or (ROOT / f"codex-{number}").exists():
        number += 1
    return f"codex-{number}"


def _add_codex(accounts: list[dict[str, str]]) -> bool:
    if shutil.which("codex") is None:
        print("Codex CLI is not installed or not on PATH.")
        return False
    name = _next_codex_name(accounts)
    home = ROOT / name
    home.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(home, 0o700)
    print(f"\nSign in to Codex subscription {name}.")
    env = dict(os.environ)
    env["CODEX_HOME"] = str(home)
    result = subprocess.run(
        ["codex", "-c", 'cli_auth_credentials_store="file"', "login"],
        env=env,
        check=False,
    )
    auth = home / "auth.json"
    if result.returncode != 0 or not auth.is_file() or auth.stat().st_size == 0:
        print(f"Codex login failed; {name} was not added.")
        return False
    os.chmod(auth, 0o600)
    accounts.append({"name": name, "auth_json": str(auth)})
    print(f"Added {name}.")
    return True


def _add_claude() -> bool:
    if shutil.which("claude") is None:
        print("Claude Code is not installed yet. Nothing changed. Add it later by running omni-rotate-accounts.")
        return False
    status = subprocess.run(
        ["claude", "auth", "status"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    if status.returncode != 0:
        print("\nSign in to the Claude Pro subscription used as the final fallback.")
        login = subprocess.run(["claude", "auth", "login"], check=False)
        if login.returncode != 0:
            print("Claude login failed; Claude fallback was not enabled.")
            return False
    verify = subprocess.run(
        ["claude", "auth", "status"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    if verify.returncode != 0:
        print("Claude is still not authenticated; Claude fallback was not enabled.")
        return False
    print("Claude Pro fallback enabled.")
    return True


def _print_route(accounts: list[dict[str, str]], claude_enabled: bool) -> None:
    print("\nCurrent route:")
    if accounts:
        for index, account in enumerate(accounts, 1):
            print(f"  {index}. Codex: {account['name']}")
    else:
        print("  (no Codex subscriptions yet)")
    if claude_enabled:
        print(f"  {len(accounts) + 1}. Claude Pro fallback")
    else:
        print("  Claude fallback: not configured")


def main() -> int:
    ROOT.mkdir(mode=0o700, parents=True, exist_ok=True)
    accounts, claude_enabled = _load()
    if claude_enabled:
        claude = shutil.which("claude")
        if claude is None:
            claude_enabled = False
            _save(accounts, claude_enabled)
        else:
            status = subprocess.run(
                [claude, "auth", "status"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
            if status.returncode != 0:
                claude_enabled = False
                _save(accounts, claude_enabled)

    print("Omni Route subscription setup")
    print("Commands: codex, claude, done")
    print("Run this setup again later whenever you want to add another subscription.")

    while True:
        _print_route(accounts, claude_enabled)
        try:
            command = input("\nAdd [codex/claude] or type done: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print("\nSetup cancelled. Successfully added subscriptions were already saved.")
            return 130

        if command in {"done", "confirm", "finish"}:
            if not accounts:
                print("Add at least one Codex subscription before finishing.")
                continue
            _save(accounts, claude_enabled)
            print("\nSubscription route saved.")
            _print_route(accounts, claude_enabled)
            return 0
        if command == "codex":
            if _add_codex(accounts):
                _save(accounts, claude_enabled)
            continue
        if command == "claude":
            if claude_enabled:
                print("Claude fallback is already configured.")
            elif _add_claude():
                claude_enabled = True
                _save(accounts, claude_enabled)
            continue
        if not command:
            continue
        print("Unknown command. Use: codex, claude, done")


if __name__ == "__main__":
    raise SystemExit(main())
