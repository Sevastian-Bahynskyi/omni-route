#!/usr/bin/env python3
"""Native desktop app lifecycle and account verification.

Rotation must never continue on the wrong account, so identity is checked
twice: cheaply from the credential store before launching, and again from the
app's own state after it comes up.

Neither check reads the GUI. For Codex the account is in `auth.json`'s
`id_token` claims; for Claude the desktop profile's account uuid equals the CLI
profile's `oauthAccount.accountUuid`.
"""
from __future__ import annotations

import base64
import json
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

CLAUDE_APP = Path("/Applications/Claude.app")
CLAUDE_BIN = CLAUDE_APP / "Contents" / "MacOS" / "Claude"
CODEX_APP = Path("/Applications/ChatGPT.app")
DESKTOP_ROOT = Path.home() / ".omnigent" / "claude-desktop"
CLAUDE_LOG = Path.home() / "Library" / "Logs" / "Claude" / "main.log"
LAUNCH_TIMEOUT = 45.0
# How long to wait for the app to declare its account after launch. Measured
# boot-to-declaration is a few seconds; the margin covers a cold start.
CONFIRM_TIMEOUT = 120.0

_ACCOUNT_LINE = re.compile(r"Initialized \{ accountId: '([0-9a-fA-F-]{36})'")


class DesktopError(RuntimeError):
    """The app could not be started, or came up as the wrong account."""


@dataclass(frozen=True)
class Identity:
    account: str | None
    source: str
    detail: str | None = None

    @property
    def known(self) -> bool:
        return bool(self.account)


def claude_user_data_dir(account: object) -> Path:
    """Where an account's desktop profile lives.

    Accepts a name or an AccountProfile. An account that is already signed into
    the app's default profile can declare that path instead of requiring a
    second sign-in for the same identity.
    """
    explicit = getattr(account, "desktop_user_data_dir", None)
    if explicit is not None:
        return Path(explicit)
    name = getattr(account, "name", account)
    return DESKTOP_ROOT / str(name)


# --------------------------------------------------------------------------
# Identity


def codex_identity(auth_json: Path) -> Identity:
    """Read the Codex account from auth.json's id_token claims.

    No process and no network: the claims are already in the file.
    """
    try:
        data = json.loads(Path(auth_json).expanduser().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return Identity(None, "auth.json", str(exc)[:120])
    token = (data.get("tokens") or {}).get("id_token")
    if not isinstance(token, str) or token.count(".") < 2:
        return Identity(None, "auth.json", "no id_token")
    payload = token.split(".")[1]
    payload += "=" * (-len(payload) % 4)
    try:
        claims = json.loads(base64.urlsafe_b64decode(payload))
    except Exception as exc:  # noqa: BLE001 - malformed token shape
        return Identity(None, "auth.json", str(exc)[:120])
    email = claims.get("email") or (claims.get("https://api.openai.com/profile") or {}).get("email")
    return Identity(email if isinstance(email, str) else None, "auth.json")


def claude_cli_identity(config_dir: Path) -> Identity:
    try:
        data = json.loads((Path(config_dir) / ".claude.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return Identity(None, "cli profile", str(exc)[:120])
    account = (data.get("oauthAccount") or {}).get("accountUuid")
    return Identity(account if isinstance(account, str) else None, "cli profile")


def claude_desktop_identity(user_data_dir: Path) -> Identity:
    """The account uuid the desktop profile is signed in as.

    The app partitions state per account, so the directory name under
    claude-code-sessions is the account uuid.
    """
    sessions = Path(user_data_dir) / "claude-code-sessions"
    if not sessions.is_dir():
        return Identity(None, "desktop profile", "profile has no sessions directory yet")
    accounts = [p for p in sessions.iterdir() if p.is_dir()]
    if not accounts:
        return Identity(None, "desktop profile", "profile has no account state yet")
    # A profile signed into a different account at some point keeps that
    # account's history, so presence alone does not identify the current
    # account. The most recently touched one does.
    accounts.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return Identity(accounts[0].name, "desktop profile",
                    f"{len(accounts)} account(s) in profile" if len(accounts) > 1 else None)


def verify_claude_account(user_data_dir: Path, config_dir: Path) -> tuple[bool, str]:
    """Pre-flight: can this account run at all?

    `CLAUDE_CONFIG_DIR` decides which account a Claude Code session runs as, so
    the credential in that profile is what must be present before launching.
    This is the Claude analogue of reading Codex's auth.json claims.

    The desktop profile is deliberately not consulted here. Its
    claude-code-sessions directory is a record of every account the profile has
    ever run as, not a statement of which one is current, so reading it before a
    launch produces a stale answer.
    """
    expected = claude_cli_identity(config_dir)
    if not expected.known:
        return False, f"no usable credential in {config_dir}: {expected.detail}"
    if not Path(user_data_dir).is_dir():
        return False, (
            f"{user_data_dir} does not exist; sign this account in once with "
            "`omni-rotate native signin`"
        )
    account_state = (
        Path(user_data_dir)
        / "claude-code-sessions"
        / (expected.account or "")
    )
    if not account_state.is_dir():
        return False, (
            f"desktop profile is not initialized for {expected.account}; "
            "sign in, open the Code tab, and create one local routine"
        )
    if not any(account_state.glob("*/scheduled-tasks.json")):
        return False, (
            f"desktop profile has no task store for {expected.account}; "
            "open Code > Routines and create one local routine"
        )
    return True, expected.account or ""


def claude_log_position(log_path: Path = CLAUDE_LOG) -> int:
    """Current end of the desktop app's log.

    Captured before a launch so the confirmation that follows reads only what
    that launch wrote. A byte offset is used rather than a timestamp because the
    log records whole seconds, which cannot separate a line written just before
    a launch from one written just after.
    """
    try:
        return log_path.stat().st_size
    except OSError:
        return 0


def claude_logged_accounts(since_position: int,
                           log_path: Path = CLAUDE_LOG) -> list[str]:
    """Accounts the app reported initialising, in order, after `since_position`.

    On startup the app writes its account into the log:

        [CCDScheduledTasks] Initialized { accountId: '<uuid>', orgId: ... }

    That is the app stating which account it loaded, which is what rotation
    needs to confirm. It is read from the log rather than from disk state
    because the app initialises an account without touching that account's
    session directory, so file mtimes report nothing for minutes.
    """
    try:
        size = log_path.stat().st_size
        with log_path.open("r", encoding="utf-8", errors="replace") as handle:
            # A rotated or truncated log invalidates the offset; read it whole.
            handle.seek(0 if size < since_position else since_position)
            text = handle.read()
    except OSError:
        return []
    return _ACCOUNT_LINE.findall(text)


def confirm_claude_account(user_data_dir: Path, config_dir: Path, *,
                           since_position: int,
                           timeout: float = CONFIRM_TIMEOUT,
                           log_path: Path = CLAUDE_LOG,
                           sleep: Callable[[float], None] = time.sleep,
                           ) -> tuple[bool, str]:
    """Post-launch: did the app actually come up as the expected account?

    Waits for the app to declare an account, because a launched process is not
    yet a loaded account: the process exists within a tenth of a second, while
    the account appears seconds later. Confirming immediately therefore fails
    every time, which reads as a wrong-account rotation when nothing is wrong.

    An account other than the expected one is a mismatch and fails at once; no
    account within the timeout is unconfirmed, which is also a failure. Both
    stop the rotation rather than continuing on an unverified account.
    """
    expected = claude_cli_identity(config_dir)
    if not expected.known:
        return False, f"cannot determine expected account: {expected.detail}"

    deadline = time.monotonic() + timeout
    while True:
        seen = claude_logged_accounts(since_position, log_path)
        if expected.account in seen:
            return True, expected.account or ""
        if seen:
            return False, (
                f"wrong account: expected {expected.account}, "
                f"the app started as {seen[-1]}"
            )
        if time.monotonic() >= deadline:
            return False, (
                f"the app did not report an account within {timeout:g}s; "
                f"cannot confirm it is running as {expected.account}"
            )
        sleep(2)


# --------------------------------------------------------------------------
# Lifecycle


def _running(pattern: str) -> list[int]:
    result = subprocess.run(["pgrep", "-f", pattern], capture_output=True, text=True)
    if result.returncode != 0:
        return []
    return [int(p) for p in result.stdout.split() if p.strip().isdigit()]


def claude_running(user_data_dir: Path | None = None) -> list[int]:
    # No leading "--": pgrep would read it as an option terminator.
    if user_data_dir is not None:
        return _running(f"user-data-dir={user_data_dir}")
    return _running(str(CLAUDE_BIN))


def codex_running() -> list[int]:
    return _running(f"{CODEX_APP}/Contents/MacOS")


def quit_app(app_name: str, *, pattern: str, timeout: float = 20.0) -> bool:
    """Ask an app to quit, then insist. Returns True when it is gone.

    A graceful quit first matters: killing mid-write can corrupt the app's own
    state, and the task store is read back on the next launch.
    """
    subprocess.run(["osascript", "-e", f'tell application "{app_name}" to quit'],
                   capture_output=True)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _running(pattern):
            return True
        time.sleep(0.5)
    subprocess.run(["pkill", "-f", f"^{pattern}"], capture_output=True)
    time.sleep(2)
    return not _running(pattern)


def quit_claude(user_data_dir: Path | None = None) -> bool:
    pattern = f"user-data-dir={user_data_dir}" if user_data_dir else str(CLAUDE_BIN)
    return quit_app("Claude", pattern=pattern)


def launch_claude(account: object, config_dir: Path | None = None,
                  *, timeout: float = LAUNCH_TIMEOUT) -> Path:
    """Start Claude Desktop under an account's own profile.

    `open -n` hands the process to launchd so it survives this process exiting;
    a backgrounded binary does not. The launch is only successful once a process
    is actually running with the expected profile -- a launch that silently fell
    back to the default profile would otherwise look identical to success, and
    would be a different account.
    """
    if not CLAUDE_APP.is_dir():
        raise DesktopError(f"{CLAUDE_APP} is not installed")
    user_data_dir = claude_user_data_dir(account)
    user_data_dir.mkdir(parents=True, exist_ok=True)

    command = ["open", "-n", "-a", str(CLAUDE_APP)]
    if config_dir is not None:
        command += ["--env", f"CLAUDE_CONFIG_DIR={config_dir}"]
    command += ["--args", f"--user-data-dir={user_data_dir}"]
    subprocess.run(command, capture_output=True, check=False)

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if claude_running(user_data_dir):
            return user_data_dir
        time.sleep(1)
    raise DesktopError(
        f"Claude did not start under {user_data_dir}; the running window is "
        "probably the default profile, which is a different account"
    )


def launch_codex(workspace: Path | None = None, *, timeout: float = LAUNCH_TIMEOUT) -> None:
    """Start the Codex desktop app.

    Codex Desktop is ChatGPT.app: the standalone Codex.app is legacy and its
    cask is deprecated upstream.
    """
    if not CODEX_APP.is_dir():
        raise DesktopError(f"{CODEX_APP} is not installed")
    command = ["open", "-a", str(CODEX_APP)]
    subprocess.run(command, capture_output=True, check=False)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if codex_running():
            return
        time.sleep(1)
    raise DesktopError("ChatGPT.app did not start")


def quit_codex() -> bool:
    return quit_app("ChatGPT", pattern=f"{CODEX_APP}/Contents/MacOS")


def bind_codex_account(auth_json: Path, codex_home: Path | None = None) -> Path:
    """Point the shared CODEX_HOME at a pooled account.

    Sessions and rollouts stay where they are; only the credential moves, which
    is why same-provider Codex rotation does not have to relocate any state.
    """
    home = Path(codex_home) if codex_home else Path.home() / ".codex"
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    target = home / "auth.json"
    source = Path(auth_json).expanduser()
    if not source.is_file():
        raise DesktopError(f"account credential missing: {source}")
    if target.exists():
        backup = home / f"auth.json.omni-route-backup-{int(time.time())}"
        target.replace(backup)
    temporary = home / ".auth.json.omni-route-tmp"
    temporary.write_bytes(source.read_bytes())
    temporary.chmod(0o600)
    temporary.replace(target)
    return target
