#!/usr/bin/env python3
"""Interactive subscription configurator for the Omnigent rotation extension."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

CONFIG = Path("~/.omnigent/codex-account-pool.json").expanduser()
STATE = Path("~/.omnigent/codex-account-pool-state.json").expanduser()
ROOT = Path("~/.omnigent/codex-accounts").expanduser()
CLAUDE_ROOT = Path("~/.omnigent/claude-accounts").expanduser()
SHARED_SKILLS = Path("~/.agents/skills").expanduser()


def _read_config() -> dict[str, object]:
    if not CONFIG.exists():
        return {}
    raw = json.loads(CONFIG.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("Account configuration must be an object")
    return raw


def _load() -> list[dict[str, object]]:
    raw = _read_config()
    accounts: list[dict[str, object]] = []
    values = raw.get("accounts", [])
    if not isinstance(values, list):
        raise ValueError("Accounts must be a list")
    for value in values:
        if not isinstance(value, dict):
            raise ValueError("Invalid account entry")
        name = value.get("name")
        provider = value.get("provider", "codex")
        credential_key = "config_dir" if provider == "claude" else "auth_json"
        credential = value.get(credential_key)
        if provider not in ("codex", "claude") or not isinstance(name, str) or not name.strip() or not isinstance(credential, str) or not credential.strip():
            raise ValueError("Invalid account profile")
        if any(account["name"] == name.strip() for account in accounts):
            raise ValueError("Duplicate account profile name")
        accounts.append({**value, "name": name.strip(), "provider": provider, credential_key: credential.strip()})
    fallback = raw.get("claude_fallback_agent")
    if isinstance(fallback, str) and fallback.strip() and not any(a["provider"] == "claude" for a in accounts):
        name = "claude-legacy"
        number = 1
        while any(a["name"] == name for a in accounts):
            name = f"claude-legacy-{number}"
            number += 1
        accounts.append({"name": name, "provider": "claude", "config_dir": os.environ.get("CLAUDE_CONFIG_DIR") or str(Path.home() / ".claude"), "use_default_config": not bool(os.environ.get("CLAUDE_CONFIG_DIR"))})
    return accounts


def _save(accounts: list[dict[str, object]]) -> None:
    payload = _read_config()
    payload.setdefault("enabled", bool(accounts))
    payload.setdefault("rotate_at_percent", 99)
    payload.pop("claude_fallback_agent", None)
    payload["accounts"] = accounts
    CONFIG.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = CONFIG.with_suffix(CONFIG.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, CONFIG)
    _sync_claude_skills(accounts)


def _sync_claude_skills(accounts: list[dict[str, object]]) -> None:
    if not SHARED_SKILLS.is_dir():
        return
    skills = [path for path in SHARED_SKILLS.iterdir() if path.is_dir() and (path / "SKILL.md").is_file()]
    for account in accounts:
        if account.get("provider") != "claude" or account.get("use_default_config") is True:
            continue
        raw_config_dir = account.get("config_dir")
        if not isinstance(raw_config_dir, str) or not raw_config_dir:
            continue
        destination = Path(raw_config_dir).expanduser() / "skills"
        destination.mkdir(parents=True, exist_ok=True, mode=0o700)
        for skill in skills:
            target = destination / skill.name
            if target.is_symlink() and target.resolve() == skill.resolve():
                continue
            if target.exists() or target.is_symlink():
                continue
            target.symlink_to(skill, target_is_directory=True)


def _next_name(accounts: list[dict[str, object]], provider: str) -> str:
    root = ROOT if provider == "codex" else CLAUDE_ROOT
    used = {a["name"] for a in accounts}
    number = 1
    while f"{provider}-{number}" in used or (root / f"{provider}-{number}").exists():
        number += 1
    return f"{provider}-{number}"


def claude_environment(home: Path, *, use_default_config: bool = False) -> dict[str, str]:
    env = dict(os.environ)
    for key in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL", "ANTHROPIC_PROFILE", "CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN_FILE_DESCRIPTOR", "CLAUDE_CODE_API_KEY_FILE_DESCRIPTOR", "CLAUDE_SECURESTORAGE_CONFIG_DIR", "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY"):
        env.pop(key, None)
    if use_default_config:
        env.pop("CLAUDE_CONFIG_DIR", None)
    else:
        env["CLAUDE_CONFIG_DIR"] = str(home)
    return env


def _add_codex(accounts: list[dict[str, object]]) -> bool:
    if shutil.which("codex") is None:
        print("Codex CLI is not installed or not on PATH.")
        return False
    name = _next_name(accounts, "codex")
    home = ROOT / name
    home.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(home, 0o700)
    print(f"\nSign in to Codex subscription {name}.")
    env = dict(os.environ)
    env["CODEX_HOME"] = str(home)
    result = subprocess.run(["codex", "-c", 'cli_auth_credentials_store="file"', "login"], env=env, check=False)
    auth = home / "auth.json"
    if result.returncode != 0 or not auth.is_file() or auth.stat().st_size == 0:
        print(f"Codex login failed; {name} was not added.")
        return False
    os.chmod(auth, 0o600)
    accounts.append({"name": name, "provider": "codex", "auth_json": str(auth)})
    print(f"Added {name}.")
    return True


def _add_claude(accounts: list[dict[str, object]]) -> bool:
    if shutil.which("claude") is None:
        print("Claude Code is not installed or not on PATH.")
        return False
    name = _next_name(accounts, "claude")
    home = CLAUDE_ROOT / name
    home.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(home, 0o700)
    env = claude_environment(home)
    print(f"\nSign in to Claude subscription {name}. Choose the account for this profile in your browser.")
    login = subprocess.run(["claude", "auth", "login", "--claudeai"], env=env, check=False)
    if login.returncode != 0:
        print(f"Claude login failed; {name} was not added.")
        return False
    verify = subprocess.run(["claude", "auth", "status", "--json"], env=env, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, check=False)
    try:
        status = json.loads(verify.stdout)
    except (ValueError, TypeError):
        status = {}
    if verify.returncode != 0 or not isinstance(status, dict) or status.get("loggedIn") is not True or status.get("authMethod") != "claude.ai":
        print(f"Claude subscription login could not be verified; {name} was not added.")
        return False
    credentials = home / ".credentials.json"
    if credentials.is_file():
        os.chmod(credentials, 0o600)
    accounts.append({"name": name, "provider": "claude", "config_dir": str(home)})
    print(f"Added {name}.")
    return True


def _print_route(accounts: list[dict[str, object]]) -> None:
    print("\nCurrent route:")
    for index, account in enumerate(accounts, 1):
        print(f"  {index}. {account['provider'].title()}: {account['name']}")
    if not accounts:
        print("  (no subscriptions yet)")


def main() -> int:
    try:
        accounts = _load()
    except (OSError, ValueError):
        print("Account configuration could not be read. Repair it before adding accounts; nothing was changed.")
        return 1
    print("Omni Route subscription setup")
    print("Commands: codex, claude, done")
    print("Add as many subscriptions of either provider as you need. Reorder them in the dashboard.")
    while True:
        _print_route(accounts)
        try:
            command = input("\nAdd [codex/claude] or type done: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print("\nSetup cancelled. Successfully added subscriptions were already saved.")
            return 130
        if command in {"done", "confirm", "finish"}:
            if not accounts:
                print("Add at least one Codex or Claude subscription before finishing.")
                continue
            _save(accounts)
            print("\nSubscription route saved.")
            _print_route(accounts)
            return 0
        if command in {"codex", "claude"}:
            if (_add_codex if command == "codex" else _add_claude)(accounts):
                _save(accounts)
            continue
        if command:
            print("Unknown command. Use: codex, claude, done")


if __name__ == "__main__":
    raise SystemExit(main())
