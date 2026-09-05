#!/usr/bin/env python3
"""The Omni Route supervisor.

Watches quota, rotates accounts at the threshold, restarts the native app and
confirms that work actually resumed. It does not own the AI session: the desktop
app does. The supervisor exists only to survive account transitions.

Two rules shape the whole file:

* **A dispatched task is not a completed task.** The desktop app reports a run
  that stalled and a run that never started identically. Rotation therefore
  verifies the effect of a resume -- the handoff marker released -- and never
  reports success because a schedule fired.
* **Never continue on the wrong account.** Identity is checked before launch and
  again afterwards. An unconfirmed account is a hard stop, not a retry loop.
"""
from __future__ import annotations

import contextlib
import json
import subprocess
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Iterator

try:
    import fcntl
except ImportError:  # pragma: no cover - POSIX only
    fcntl = None  # type: ignore[assignment]

import desktop
import handoff
import native_scheduler as ns
import quota
from account_pool import AccountPool, AccountProfile, PoolConfig, preparation_percent

STATE_DIR = Path.home() / ".omnigent"
LOCK_PATH = STATE_DIR / "omni-route-supervisor.lock"
STATUS_PATH = STATE_DIR / "omni-route-supervisor-status.json"
REQUEST_NAME = "rotation-request.json"

WRAP_UP_TIMEOUT = 600.0      # 10 minutes, then the restart is forced
RESUME_TIMEOUT = 480.0       # 8 minutes for the automation to pick the handoff up
RESTART_ATTEMPTS = 2         # retries before the headless fallback


class Phase(str, Enum):
    NORMAL = "normal"
    PREPARE = "prepare"
    SWITCH = "switch"


class Outcome(str, Enum):
    OK = "ok"
    DEGRADED = "degraded"          # resumed, but not the way it should have
    NEEDS_USER_ACTION = "needs_user_action"
    FAILED = "failed"


class SupervisorError(RuntimeError):
    pass


@dataclass
class RotationResult:
    outcome: Outcome
    detail: str
    from_account: str | None = None
    to_account: str | None = None
    unclean: bool = False
    events: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "outcome": self.outcome.value,
            "detail": self.detail,
            "fromAccount": self.from_account,
            "toAccount": self.to_account,
            "unclean": self.unclean,
            "events": self.events,
        }


@contextlib.contextmanager
def rotation_lock(timeout: float = 0.0) -> Iterator[None]:
    """Serialise rotations.

    A manual switch and an automatic one must never interleave: both quit the
    app and rewrite credentials, and the loser of that race would leave the
    session on an account nobody selected.
    """
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with LOCK_PATH.open("a+", encoding="utf-8") as lock:
        if fcntl is None:
            yield
            return
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise SupervisorError("another rotation is already running") from None
                time.sleep(0.25)
        try:
            yield
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def evaluate(usage: quota.Usage, *, rotate_at_percent: float) -> Phase:
    """Decide what a usage reading means.

    An unknown reading is deliberately inert. Rotating on a failed scrape would
    burn an account for no reason, so the supervisor holds until it has a number.
    """
    percent = usage.decisive_percent
    if not usage.known or percent is None:
        return Phase.NORMAL
    if percent >= rotate_at_percent:
        return Phase.SWITCH
    if percent >= preparation_percent(rotate_at_percent):
        return Phase.PREPARE
    return Phase.NORMAL


def write_request(workspace: Path, phase: Phase, *, generation: int,
                  to_account: str | None = None) -> Path:
    """Arm the Stop hook, which acts at the next real turn boundary."""
    root = Path(workspace) / handoff.DIRNAME
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = root / REQUEST_NAME
    path.write_text(json.dumps({
        "phase": phase.value,
        "generation": generation,
        "to_account": to_account,
        "armed_at": int(time.time()),
    }), encoding="utf-8")
    return path


def clear_request(workspace: Path) -> None:
    path = Path(workspace) / handoff.DIRNAME / REQUEST_NAME
    if path.exists():
        path.unlink()


def request_generation(workspace: Path) -> int:
    path = Path(workspace) / handoff.DIRNAME / REQUEST_NAME
    if not path.exists():
        return 0
    try:
        return int(json.loads(path.read_text(encoding="utf-8")).get("generation", 0))
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return 0


class Supervisor:
    def __init__(self, workspace: Path, *, pool: AccountPool | None = None,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self.workspace = Path(workspace).resolve()
        self.pool = pool or AccountPool(PoolConfig.load())
        self._sleep = sleep

    # -- status ---------------------------------------------------------

    def status(self) -> dict[str, Any]:
        config = self.pool.config
        state = self.pool.snapshot() if self.pool.enabled else {}
        return {
            "workspace": str(self.workspace),
            "enabled": self.pool.enabled,
            "rotateAtPercent": config.rotate_at_percent,
            "preparationAtPercent": config.preparation_at_percent,
            "currentAccount": state.get("current_account"),
            "cooldowns": state.get("cooldowns", {}),
            "accounts": [
                {"name": a.name, "provider": a.provider} for a in config.accounts
            ],
        }

    def publish_status(self, extra: dict[str, Any] | None = None) -> None:
        payload = self.status()
        if extra:
            payload.update(extra)
        payload["updatedAt"] = int(time.time())
        STATUS_PATH.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = STATUS_PATH.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        temporary.replace(STATUS_PATH)

    # -- rotation -------------------------------------------------------

    def wait_for_handoff(self, *, timeout: float = WRAP_UP_TIMEOUT) -> bool:
        """Wait for the outgoing agent to finish and write its handoff.

        Returns False on timeout, which makes the restart unclean rather than
        impossible: a turn that never ends must not pin the session to a dead
        account forever.
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if handoff.is_pending(self.workspace):
                return True
            self._sleep(2)
        return False

    def wait_for_resume(self, *, timeout: float = RESUME_TIMEOUT) -> bool:
        """Wait for evidence that the new account actually picked the work up.

        The marker is released only by a run that finished, so this observes the
        effect of the resume rather than the fact that a task was dispatched.
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not handoff.is_pending(self.workspace):
                return True
            self._sleep(5)
        return False

    def prepare_account(self, profile: AccountProfile) -> None:
        """Everything that must be true before the app is allowed to start."""
        if profile.provider == "codex":
            if profile.auth_json is None:
                raise SupervisorError(f"{profile.name} has no auth.json")
            desktop.bind_codex_account(profile.auth_json)
            return
        if profile.config_dir is None:
            raise SupervisorError(f"{profile.name} has no config_dir")
        # Registering the automation also records workspace trust and the
        # permissions a scheduled session needs. Without either, the run stalls
        # silently and is reported as "Skipped".
        ns.install(
            desktop.claude_user_data_dir(profile),
            self.workspace,
            claude_config_dir=profile.config_dir,
        )

    def verify_account(self, profile: AccountProfile) -> tuple[bool, str]:
        if profile.provider == "codex":
            identity = desktop.codex_identity(profile.auth_json) if profile.auth_json else None
            if identity is None or not identity.known:
                return False, "could not read the Codex account identity"
            return True, identity.account or ""
        return desktop.verify_claude_account(
            desktop.claude_user_data_dir(profile), profile.config_dir or Path()
        )

    def confirm_account(self, profile: AccountProfile, *, since: float) -> tuple[bool, str]:
        """Confirm after launch that the app really came up on this account."""
        if profile.provider == "codex":
            identity = desktop.codex_identity(profile.auth_json) if profile.auth_json else None
            if identity is None or not identity.known:
                return False, "could not read the Codex account identity"
            return True, identity.account or ""
        return desktop.confirm_claude_account(
            desktop.claude_user_data_dir(profile), profile.config_dir or Path(),
            since=since,
        )

    def start_provider(self, profile: AccountProfile) -> None:
        if profile.provider == "codex":
            desktop.launch_codex(self.workspace)
        else:
            desktop.launch_claude(profile, profile.config_dir)

    def stop_provider(self, profile: AccountProfile | None) -> None:
        if profile is not None and profile.provider == "codex":
            desktop.quit_codex()
        elif profile is not None:
            desktop.quit_claude(desktop.claude_user_data_dir(profile))
        else:
            desktop.quit_claude()
            desktop.quit_codex()

    def headless_continuation(self, profile: AccountProfile) -> bool:
        """Run exactly one continuation turn outside the desktop app.

        This is the fenced exception to desktop-only operation. It runs only
        after the restarts have failed, does one turn, and hands straight back
        to the app; the rotation is reported as degraded, never as clean.
        """
        prompt = ns.rotation_prompt(self.workspace)
        try:
            if profile.provider == "claude":
                env_dir = profile.config_dir
                command = ["claude", "-p", prompt, "--permission-mode", "acceptEdits"]
                env = {"CLAUDE_CONFIG_DIR": str(env_dir)} if env_dir else {}
            else:
                command = ["codex", "exec", prompt]
                env = {}
            import os
            merged = {**os.environ, **env}
            subprocess.run(command, cwd=str(self.workspace), env=merged,
                           capture_output=True, timeout=900, check=False)
        except (OSError, subprocess.SubprocessError) as exc:
            return False
        return not handoff.is_pending(self.workspace)

    def rotate(self, *, reason: str = "threshold reached",
               exhausted: AccountProfile | None = None) -> RotationResult:
        """Perform one rotation, end to end."""
        events: list[str] = []
        with rotation_lock(timeout=5.0):
            generation = request_generation(self.workspace) + 1
            current = exhausted
            write_request(self.workspace, Phase.SWITCH, generation=generation,
                          to_account=None)
            events.append("armed switch request")

            clean = self.wait_for_handoff()
            events.append("handoff written" if clean else "wrap-up timed out; forcing restart")

            nxt = self.pool.rotate_session(
                str(self.workspace),
                exhausted_account=current.name if current else None,
                retry_at=None,
                reason=reason,
                fallback_to_other_providers=True,
            )
            if nxt is None:
                clear_request(self.workspace)
                return RotationResult(
                    Outcome.NEEDS_USER_ACTION,
                    "no account is available; all are exhausted or unauthenticated",
                    from_account=current.name if current else None,
                    events=events,
                )
            events.append(f"selected {nxt.name}")

            self.stop_provider(current)
            # The incoming account's app must be stopped as well. It may already
            # be running -- Claude Desktop's default profile usually is -- and it
            # holds its task store in memory, so an automation written while it
            # runs is discarded when the app next saves.
            self.stop_provider(nxt)
            events.append("stopped previous and incoming apps")

            try:
                self.prepare_account(nxt)
            except (SupervisorError, ns.SchedulerError, desktop.DesktopError) as exc:
                clear_request(self.workspace)
                return RotationResult(
                    Outcome.NEEDS_USER_ACTION, f"could not prepare {nxt.name}: {exc}",
                    from_account=current.name if current else None,
                    to_account=nxt.name, events=events,
                )

            ok, detail = self.verify_account(nxt)
            if not ok:
                clear_request(self.workspace)
                return RotationResult(
                    Outcome.NEEDS_USER_ACTION,
                    f"pre-flight identity check failed for {nxt.name}: {detail}",
                    from_account=current.name if current else None,
                    to_account=nxt.name, events=events,
                )
            events.append(f"pre-flight identity ok ({detail})")

            for attempt in range(1, RESTART_ATTEMPTS + 1):
                launched_at = time.time()
                try:
                    self.start_provider(nxt)
                except desktop.DesktopError as exc:
                    events.append(f"launch attempt {attempt} failed: {exc}")
                    continue
                events.append(f"launched {nxt.provider} (attempt {attempt})")
                confirmed, confirm_detail = self.confirm_account(nxt, since=launched_at)
                if not confirmed:
                    # Never treat an unconfirmed account as a success. Work that
                    # continues on the wrong account is the failure this whole
                    # sequence exists to prevent, and it is invisible unless the
                    # rotation refuses it here.
                    events.append(f"account NOT confirmed: {confirm_detail}")
                    self.stop_provider(nxt)
                    continue
                events.append(f"account confirmed ({confirm_detail})")
                if self.wait_for_resume():
                    clear_request(self.workspace)
                    return RotationResult(
                        Outcome.OK, f"resumed on {nxt.name}",
                        from_account=current.name if current else None,
                        to_account=nxt.name, unclean=not clean, events=events,
                    )
                events.append(f"no resume after attempt {attempt}")
                self.stop_provider(nxt)

            if any("NOT confirmed" in event for event in events):
                clear_request(self.workspace)
                return RotationResult(
                    Outcome.NEEDS_USER_ACTION,
                    f"{nxt.name} did not come up as the expected account; "
                    "refusing to continue on the wrong account",
                    from_account=current.name if current else None,
                    to_account=nxt.name, unclean=True, events=events,
                )

            events.append("restarts exhausted; running one headless continuation")
            if self.headless_continuation(nxt):
                with contextlib.suppress(desktop.DesktopError):
                    self.start_provider(nxt)
                clear_request(self.workspace)
                return RotationResult(
                    Outcome.DEGRADED,
                    f"resumed on {nxt.name} outside the desktop app after "
                    "the app failed to start a turn",
                    from_account=current.name if current else None,
                    to_account=nxt.name, unclean=True, events=events,
                )

            clear_request(self.workspace)
            return RotationResult(
                Outcome.NEEDS_USER_ACTION,
                f"{nxt.name} did not start any turn; manual attention needed",
                from_account=current.name if current else None,
                to_account=nxt.name, unclean=True, events=events,
            )

    # -- polling --------------------------------------------------------

    def poll_once(self, profile: AccountProfile) -> tuple[Phase, quota.Usage]:
        usage = quota.read_usage(profile)
        phase = evaluate(usage, rotate_at_percent=self.pool.config.rotate_at_percent)
        if phase is Phase.PREPARE:
            generation = request_generation(self.workspace) + 1
            if request_generation(self.workspace) == 0:
                write_request(self.workspace, Phase.PREPARE, generation=generation)
        self.publish_status({"phase": phase.value, "usage": usage.to_dict(),
                             "account": profile.name})
        return phase, usage
