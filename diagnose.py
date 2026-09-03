#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import importlib.util
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

HOME = Path.home()
CONFIG_PATH = HOME / ".omnigent" / "codex-account-pool.json"
STATE_PATH = HOME / ".omnigent" / "codex-account-pool-state.json"
BRIDGE_ROOT = HOME / ".omnigent" / "codex-native"
BASE = HOME / ".local" / "share" / "omnigent-subscription-rotation"
SRC = BASE / "omnigent"
VENV_PYTHON = SRC / ".venv" / "bin" / "python"
PATCHED_OMNI = SRC / ".venv" / "bin" / "omni"
BIN = HOME / ".local" / "bin"

PASS = "PASS"
WARN = "WARN"
FAIL = "FAIL"
INFO = "INFO"


class Reporter:
    def __init__(self) -> None:
        self.counts = {PASS: 0, WARN: 0, FAIL: 0, INFO: 0}

    def emit(self, level: str, label: str, detail: str = "") -> None:
        self.counts[level] = self.counts.get(level, 0) + 1
        suffix = f" :: {detail}" if detail else ""
        print(f"[{level}] {label}{suffix}")

    def summary(self) -> int:
        print()
        print("=== OMNI ROUTE DIAGNOSTIC SUMMARY ===")
        print(f"PASS {self.counts[PASS]}  WARN {self.counts[WARN]}  FAIL {self.counts[FAIL]}")
        if self.counts[FAIL]:
            print("RESULT: NOT READY")
            return 1
        if self.counts[WARN]:
            print("RESULT: READY WITH WARNINGS")
            return 0
        print("RESULT: READY")
        return 0


def read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def b64url_json(segment: str) -> dict[str, Any] | None:
    try:
        raw = base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4))
        value = json.loads(raw.decode("utf-8"))
    except Exception:
        return None
    return value if isinstance(value, dict) else None


def jwt_payload(token: object) -> dict[str, Any] | None:
    if not isinstance(token, str):
        return None
    parts = token.split(".")
    if len(parts) != 3:
        return None
    return b64url_json(parts[1])


def find_string(value: object, keys: set[str]) -> str | None:
    if isinstance(value, dict):
        for key, item in value.items():
            normalized = str(key).casefold().replace("-", "_")
            if normalized in keys and isinstance(item, str) and item.strip():
                return item.strip()
        for item in value.values():
            found = find_string(item, keys)
            if found:
                return found
    elif isinstance(value, list):
        for item in value:
            found = find_string(item, keys)
            if found:
                return found
    return None


def identity_from_auth(path: Path) -> dict[str, str | None]:
    data = read_json(path)
    email = find_string(data, {"email", "user_email"})
    account_id = find_string(
        data,
        {
            "account_id",
            "accountid",
            "chatgpt_account_id",
            "chatgptaccountid",
            "workspace_id",
            "workspaceid",
        },
    )
    subject = None

    tokens: list[str] = []

    def collect_tokens(value: object) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                if (
                    isinstance(item, str)
                    and str(key).casefold().replace("-", "_") in {"id_token", "access_token"}
                ):
                    tokens.append(item)
                else:
                    collect_tokens(item)
        elif isinstance(value, list):
            for item in value:
                collect_tokens(item)

    collect_tokens(data)
    for token in tokens:
        payload = jwt_payload(token)
        if not payload:
            continue
        email = email or find_string(payload, {"email", "user_email"})
        account_id = account_id or find_string(
            payload,
            {
                "account_id",
                "accountid",
                "chatgpt_account_id",
                "chatgptaccountid",
                "workspace_id",
                "workspaceid",
            },
        )
        if subject is None and isinstance(payload.get("sub"), str):
            subject = payload["sub"]
    return {"email": email, "account_id": account_id, "subject": subject}


def run(
    args: list[str],
    *,
    env: dict[str, str] | None = None,
    timeout: float = 10.0,
) -> subprocess.CompletedProcess[str] | None:
    try:
        return subprocess.run(
            args,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None


def command_check(rep: Reporter, command: str, required: bool = True) -> str | None:
    path = shutil.which(command)
    if path:
        rep.emit(PASS, f"command {command}", path)
        return path
    rep.emit(FAIL if required else WARN, f"command {command}", "not found")
    return None


def test_synthetic_rotation(rep: Reporter) -> None:
    if not VENV_PYTHON.is_file():
        rep.emit(FAIL, "synthetic rotation", "patched Python environment missing")
        return
    code = r'''
import json, tempfile
from pathlib import Path
from omnigent.codex_account_pool import AccountProfile, CodexAccountPool, PoolConfig, decide_rate_limits, is_usage_limit_payload
with tempfile.TemporaryDirectory() as td:
    td=Path(td)
    def auth(name, token):
        p=td/name
        p.write_text(json.dumps({"tokens":{"access_token":token,"refresh_token":"r"}}))
        return p
    a=auth("a.json","a"); b=auth("b.json","b")
    pool=CodexAccountPool(
        PoolConfig(accounts=(AccountProfile("a",a),AccountProfile("b",b))),
        state_path=td/"state.json",
        now=lambda:1000,
    )
    assert pool.account_for_session("test").name=="a"
    assert pool.rotate_session("test", exhausted_account="a", retry_at=2000, reason="diagnostic").name=="b"
    d=decide_rate_limits({"result":{"ordinaryUsageAllowed":True,"rateLimits":{"primary":{"usedPercent":99.5,"resetsAt":2000}}}}, rotate_at_percent=99)
    assert d.rotate and d.retry_at==2000
    assert is_usage_limit_payload({"error":{"codexErrorInfo":"usageLimitExceeded"}})
print("ok")
'''
    result = run([str(VENV_PYTHON), "-c", code], timeout=10)
    if result and result.returncode == 0 and result.stdout.strip() == "ok":
        rep.emit(PASS, "synthetic A -> B rotation", "real pool implementation passed")
    else:
        detail = (result.stderr.strip() if result else "could not execute")[:220]
        rep.emit(FAIL, "synthetic A -> B rotation", detail)


def test_wiring(rep: Reporter) -> None:
    checks = [
        (SRC / "omnigent" / "codex_native_app_server.py", "auth_json_source", "per-session auth injection"),
        (SRC / "omnigent" / "inner" / "codex_native_executor.py", "preflight_rotation_request", "quota preflight"),
        (SRC / "omnigent" / "codex_native_forwarder.py", "request_rotation_from_usage_error", "mid-turn quota detection"),
        (SRC / "omnigent" / "runner" / "native" / "orchestration.py", "ensure_rotation_monitor", "rotation monitor"),
        (SRC / "omnigent" / "codex_account_rotation.py", "switch-agent", "Claude handoff path"),
    ]
    for path, needle, label in checks:
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            rep.emit(FAIL, f"runtime wiring: {label}", f"missing {path.name}")
            continue
        if needle in text:
            rep.emit(PASS, f"runtime wiring: {label}")
        else:
            rep.emit(FAIL, f"runtime wiring: {label}", f"marker {needle!r} missing")


def test_live_bridge(rep: Reporter, configured_names: set[str]) -> None:
    if not BRIDGE_ROOT.is_dir():
        rep.emit(WARN, "live Codex bridge", "no bridge directory yet; start `omni-rotate codex`")
        return

    bridge_dirs = sorted(
        {p.parent for p in BRIDGE_ROOT.glob("*/state.json")},
        key=lambda p: p.stat().st_mtime if p.exists() else 0,
        reverse=True,
    )
    if not bridge_dirs:
        rep.emit(WARN, "live Codex bridge", "no active/resumable bridge state found")
        return

    chosen = None
    for bridge in bridge_dirs:
        state = read_json(bridge / "state.json")
        socket_path = state.get("socket_path")
        if isinstance(socket_path, str) and Path(socket_path).exists():
            chosen = bridge
            break
    if chosen is None:
        rep.emit(WARN, "live Codex app-server", "bridge state exists but no live app-server socket")
        return

    runtime = read_json(chosen / "codex-account-runtime.json")
    account_name = runtime.get("account_name")
    if isinstance(account_name, str) and account_name in configured_names:
        rep.emit(PASS, "live bridge account binding", account_name)
    elif isinstance(account_name, str):
        rep.emit(FAIL, "live bridge account binding", f"unknown account {account_name}")
    else:
        rep.emit(WARN, "live bridge account binding", "runtime account marker missing")

    helper = r'''
import asyncio, json, sys
from pathlib import Path
from omnigent.codex_native_app_server import client_for_transport
from omnigent.codex_native_bridge import read_bridge_state
async def main():
    bridge=Path(sys.argv[1])
    state=read_bridge_state(bridge)
    if state is None:
        raise RuntimeError("bridge state unreadable")
    client=client_for_transport(state.socket_path, client_name="omni-route-diagnostic")
    await client.connect()
    try:
        account=await client.request("account/read", {"refreshToken": False})
        quota=await client.request("account/rateLimits/read", {})
        print(json.dumps({"account": account, "quota": quota}))
    finally:
        await client.close()
asyncio.run(main())
'''
    result = run([str(VENV_PYTHON), "-c", helper, str(chosen)], timeout=8)
    if not result or result.returncode != 0:
        detail = (result.stderr.strip() if result else "connection failed")[:240]
        rep.emit(FAIL, "live Codex account/quota API", detail)
        return

    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        rep.emit(FAIL, "live Codex account/quota API", "malformed response")
        return

    account = payload.get("account")
    quota = payload.get("quota")
    rep.emit(
        PASS if isinstance(account, dict) and "result" in account else FAIL,
        "live account/read",
        "Codex app-server responded" if isinstance(account, dict) and "result" in account else "unexpected response shape",
    )

    if isinstance(quota, dict) and "result" in quota:
        detail = "Codex app-server responded"
        result_obj = quota.get("result")
        if isinstance(result_obj, dict):
            rl = result_obj.get("rateLimits")
            if isinstance(rl, dict):
                percents = []
                for key in ("primary", "secondary"):
                    window = rl.get(key)
                    if isinstance(window, dict) and isinstance(window.get("usedPercent"), (int, float)):
                        percents.append(f"{key}={window['usedPercent']}%")
                if percents:
                    detail += " // " + ", ".join(percents)
        rep.emit(PASS, "live account/rateLimits/read", detail)
    else:
        rep.emit(FAIL, "live account/rateLimits/read", "unexpected response shape")


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only Omni Route diagnostics")
    parser.parse_args()

    rep = Reporter()
    print("OMNI ROUTE // READ-ONLY DIAGNOSTIC")
    print(time.strftime("%Y-%m-%d %H:%M:%S %Z"))
    print()

    rep.emit(PASS if sys.platform == "darwin" else FAIL, "platform", "macOS" if sys.platform == "darwin" else sys.platform)

    codex = command_check(rep, "codex", required=True)
    command_check(rep, "uv", required=True)
    command_check(rep, "tmux", required=True)
    command_check(rep, "omni", required=False)

    for name in ("omni-rotate", "omni-rotate-accounts", "omni-rotate-status"):
        path = BIN / name
        rep.emit(PASS if path.is_file() else FAIL, f"launcher {name}", str(path))

    if SRC.is_dir() and VENV_PYTHON.is_file() and PATCHED_OMNI.is_file():
        rep.emit(PASS, "patched Omnigent runtime", str(SRC))
        version = run([str(PATCHED_OMNI), "--version"], timeout=8)
        if version and version.returncode == 0:
            text = (version.stdout or version.stderr).strip().splitlines()
            rep.emit(PASS, "patched omni executable", text[-1] if text else "responded")
        else:
            rep.emit(FAIL, "patched omni executable", "could not run --version")
    else:
        rep.emit(FAIL, "patched Omnigent runtime", "installation incomplete")

    test_wiring(rep)
    test_synthetic_rotation(rep)

    config = read_json(CONFIG_PATH)
    if not config:
        rep.emit(FAIL, "account pool config", f"missing/unreadable {CONFIG_PATH}")
        return rep.summary()
    rep.emit(PASS, "account pool config", str(CONFIG_PATH))

    threshold = config.get("rotate_at_percent")
    if isinstance(threshold, (int, float)) and 0 < float(threshold) <= 100:
        rep.emit(PASS, "rotation threshold", f"{threshold}%")
    else:
        rep.emit(FAIL, "rotation threshold", repr(threshold))

    accounts_raw = config.get("accounts")
    accounts = accounts_raw if isinstance(accounts_raw, list) else []
    rep.emit(PASS if accounts else FAIL, "configured Codex accounts", str(len(accounts)) if accounts else "none")

    identities: dict[str, list[str]] = {}
    configured_names: set[str] = set()
    for index, item in enumerate(accounts, 1):
        if not isinstance(item, dict):
            rep.emit(FAIL, f"account #{index}", "invalid config entry")
            continue
        name = item.get("name")
        auth_raw = item.get("auth_json")
        if not isinstance(name, str) or not name:
            rep.emit(FAIL, f"account #{index}", "missing name")
            continue
        configured_names.add(name)
        if not isinstance(auth_raw, str) or not auth_raw:
            rep.emit(FAIL, f"account {name}", "missing auth_json")
            continue
        auth_path = Path(auth_raw).expanduser()
        if not auth_path.is_file() or auth_path.stat().st_size == 0:
            rep.emit(FAIL, f"account {name} auth", "missing/empty auth.json")
            continue

        identity = identity_from_auth(auth_path)
        email = identity["email"] or "email unavailable"
        rep.emit(PASS, f"account {name} auth file", email)

        try:
            mode = stat.S_IMODE(auth_path.stat().st_mode)
            rep.emit(WARN if mode & 0o077 else PASS, f"account {name} auth permissions", oct(mode))
        except OSError:
            rep.emit(WARN, f"account {name} auth permissions", "could not stat")

        stable_identity = identity["account_id"] or identity["subject"] or identity["email"]
        if stable_identity:
            identities.setdefault(stable_identity, []).append(name)

        if codex:
            env = os.environ.copy()
            env["CODEX_HOME"] = str(auth_path.parent)
            result = run(
                [codex, "-c", 'cli_auth_credentials_store="file"', "login", "status"],
                env=env,
                timeout=10,
            )
            if result and result.returncode == 0:
                status_text = (result.stdout or result.stderr).strip().replace("\n", " ")
                rep.emit(PASS, f"account {name} Codex login", status_text[:180] or email)
            else:
                detail = ((result.stderr or result.stdout).strip() if result else "command failed")[:180]
                rep.emit(FAIL, f"account {name} Codex login", detail)

    duplicates = {key: names for key, names in identities.items() if len(names) > 1}
    if duplicates:
        for names in duplicates.values():
            rep.emit(FAIL, "duplicate Codex identity", ", ".join(names))
    elif len(accounts) > 1:
        rep.emit(PASS, "Codex account identities", "registered profiles appear distinct")

    state = read_json(STATE_PATH)
    if STATE_PATH.exists() and not state:
        rep.emit(FAIL, "pool state", "exists but is unreadable")
    elif state:
        current = state.get("current_account")
        if current is None:
            rep.emit(INFO, "current account", "none selected yet")
        elif isinstance(current, str) and current in configured_names:
            rep.emit(PASS, "current account", current)
        else:
            rep.emit(FAIL, "current account", f"unknown value {current!r}")

        cooldowns = state.get("cooldowns")
        if isinstance(cooldowns, dict):
            now = int(time.time())
            active = []
            for name, value in cooldowns.items():
                if isinstance(value, dict) and isinstance(value.get("retry_at"), (int, float)):
                    retry = int(value["retry_at"])
                    if retry > now:
                        active.append(f"{name} until {time.strftime('%H:%M:%S', time.localtime(retry))}")
            rep.emit(INFO, "active cooldowns", ", ".join(active) if active else "none")
        else:
            rep.emit(WARN, "pool cooldown state", "unexpected shape")
    else:
        rep.emit(INFO, "pool state", "not created yet; first routed session will create it")

    claude_agent = config.get("claude_fallback_agent")
    if isinstance(claude_agent, str) and claude_agent.strip():
        claude = shutil.which("claude")
        if not claude:
            rep.emit(FAIL, "Claude fallback", "configured but Claude CLI missing")
        else:
            result = run([claude, "auth", "status"], timeout=8)
            rep.emit(
                PASS if result and result.returncode == 0 else FAIL,
                "Claude fallback",
                "configured and authenticated" if result and result.returncode == 0 else "configured but not authenticated",
            )
    else:
        rep.emit(WARN, "Claude fallback", "not configured (optional)")

    test_live_bridge(rep, configured_names)
    return rep.summary()


if __name__ == "__main__":
    raise SystemExit(main())
