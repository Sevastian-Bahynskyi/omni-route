#!/usr/bin/env python3
"""Rotation handoff: the task state that survives an account or provider switch.

Written by the outgoing agent at the preparation threshold, while quota headroom
still exists. Delivered to the incoming agent by pointer, never pasted: the
repository and Git state remain the source of truth.

Layout, inside the workspace and git-ignored:

    .omni-route/handoff-<timestamp>.md   the handoff itself
    .omni-route/handoff-latest.md        symlink to the newest
    .omni-route/handoff-pending          marker consumed by the incoming agent
"""
from __future__ import annotations

import json
import subprocess
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

DIRNAME = ".omni-route"
PENDING = "handoff-pending"
INFLIGHT = "handoff-inflight"
LATEST = "handoff-latest.md"

# The seven fields required by NATIVE_HARNESS_DIRECTION.md, plus worktree_path
# and branch, which Claude Desktop's per-session worktrees make mandatory.
FIELDS = (
    "goal",
    "progress",
    "decisions",
    "files",
    "status",
    "blockers",
    "next_action",
    "worktree_path",
    "branch",
)


@dataclass
class Handoff:
    goal: str = ""
    progress: str = ""
    decisions: str = ""
    files: str = ""
    status: str = ""
    blockers: str = ""
    next_action: str = ""
    worktree_path: str = ""
    branch: str = ""
    commit: str = ""
    from_account: str = ""
    to_account: str = ""
    created_at: int = field(default_factory=lambda: int(time.time()))

    def to_markdown(self) -> str:
        meta = {
            "worktree_path": self.worktree_path,
            "branch": self.branch,
            "commit": self.commit,
            "from_account": self.from_account,
            "to_account": self.to_account,
            "created_at": self.created_at,
        }
        body = [
            "---",
            json.dumps(meta, indent=2),
            "---",
            "",
            "# Rotation handoff",
            "",
            "Read this, then verify the repository and Git state yourself.",
            "The repository is the source of truth; this file is only a pointer.",
            "",
            f"## Goal\n\n{self.goal or '(not recorded)'}",
            f"## Progress\n\n{self.progress or '(not recorded)'}",
            f"## Decisions\n\n{self.decisions or '(not recorded)'}",
            f"## Changed / relevant files\n\n{self.files or '(not recorded)'}",
            f"## Test and status\n\n{self.status or '(not recorded)'}",
            f"## Blockers\n\n{self.blockers or '(none)'}",
            f"## Exact next action\n\n{self.next_action or '(not recorded)'}",
        ]
        return "\n\n".join(body).rstrip() + "\n"

    @classmethod
    def from_markdown(cls, text: str) -> "Handoff":
        instance = cls()
        if text.startswith("---"):
            parts = text.split("---", 2)
            if len(parts) >= 3:
                try:
                    meta = json.loads(parts[1])
                except json.JSONDecodeError:
                    meta = {}
                for key, value in meta.items() if isinstance(meta, dict) else []:
                    if hasattr(instance, key):
                        setattr(instance, key, value)
                text = parts[2]
        headings = {
            "Goal": "goal",
            "Progress": "progress",
            "Decisions": "decisions",
            "Changed / relevant files": "files",
            "Test and status": "status",
            "Blockers": "blockers",
            "Exact next action": "next_action",
        }
        current: str | None = None
        buffer: list[str] = []
        for line in text.splitlines():
            if line.startswith("## "):
                if current:
                    setattr(instance, current, "\n".join(buffer).strip())
                current = headings.get(line[3:].strip())
                buffer = []
            elif current:
                buffer.append(line)
        if current:
            setattr(instance, current, "\n".join(buffer).strip())
        for name in FIELDS:
            if getattr(instance, name, "") in {"(not recorded)", "(none)"}:
                setattr(instance, name, "")
        return instance


def directory(workspace: Path) -> Path:
    return Path(workspace) / DIRNAME


def git(workspace: Path, *args: str) -> str:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=str(workspace),
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


def git_context(workspace: Path) -> dict[str, str]:
    """Branch, worktree root and HEAD for the given workspace."""
    return {
        "branch": git(workspace, "rev-parse", "--abbrev-ref", "HEAD"),
        "worktree_path": git(workspace, "rev-parse", "--show-toplevel") or str(workspace),
        "commit": git(workspace, "rev-parse", "HEAD"),
    }


def is_dirty(workspace: Path) -> bool:
    return bool(git(workspace, "status", "--porcelain"))


def write(workspace: Path, handoff: Handoff, *, mark_pending: bool = True) -> Path:
    """Write a handoff and, by default, arm it for the incoming agent."""
    root = directory(workspace)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    for key, value in git_context(workspace).items():
        if not getattr(handoff, key, ""):
            setattr(handoff, key, value)
    target = root / f"handoff-{handoff.created_at}.md"
    target.write_text(handoff.to_markdown(), encoding="utf-8")
    latest = root / LATEST
    if latest.exists() or latest.is_symlink():
        latest.unlink()
    latest.write_text(handoff.to_markdown(), encoding="utf-8")
    if mark_pending:
        arm(workspace, handoff)
    return target


def arm(workspace: Path, handoff: Handoff | None = None) -> Path:
    """Create the pending marker the self-gating automation looks for."""
    root = directory(workspace)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    marker = root / PENDING
    payload: dict[str, Any] = {"armed_at": int(time.time())}
    if handoff is not None:
        payload.update(
            {"to_account": handoff.to_account, "from_account": handoff.from_account}
        )
    marker.write_text(json.dumps(payload), encoding="utf-8")
    return marker


def is_pending(workspace: Path) -> bool:
    return (directory(workspace) / PENDING).exists()


def is_inflight(workspace: Path) -> bool:
    """A run has claimed the handoff but has not finished it."""
    return (directory(workspace) / INFLIGHT).exists()


def inflight_age(workspace: Path) -> float | None:
    """Seconds since a run claimed the handoff, or None if none is in flight."""
    marker = directory(workspace) / INFLIGHT
    if not marker.exists():
        return None
    return time.time() - marker.stat().st_mtime


def recover_stalled(workspace: Path, *, older_than: float = 1800) -> bool:
    """Return an abandoned in-flight handoff to pending.

    A run that claims the handoff and then dies would otherwise strand the task
    forever, because the marker it consumed is the only thing that makes the
    automation act.
    """
    age = inflight_age(workspace)
    if age is None or age < older_than:
        return False
    (directory(workspace) / INFLIGHT).replace(directory(workspace) / PENDING)
    return True


def consume(workspace: Path) -> Handoff | None:
    """Read the latest handoff and clear the pending marker."""
    latest = directory(workspace) / LATEST
    if not latest.exists():
        clear(workspace)
        return None
    handoff = Handoff.from_markdown(latest.read_text(encoding="utf-8"))
    clear(workspace)
    return handoff


def clear(workspace: Path) -> None:
    marker = directory(workspace) / PENDING
    if marker.exists():
        marker.unlink()


def read_latest(workspace: Path) -> Handoff | None:
    latest = directory(workspace) / LATEST
    if not latest.exists():
        return None
    return Handoff.from_markdown(latest.read_text(encoding="utf-8"))
