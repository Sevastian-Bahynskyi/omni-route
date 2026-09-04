#!/usr/bin/env python3
"""Register the self-gating rotation automation in Claude Desktop.

Claude Desktop stores a scheduled task in two places:

  * the prompt, at ``~/.claude/scheduled-tasks/<id>/SKILL.md``
    (or under ``CLAUDE_CONFIG_DIR`` when set);
  * the metadata, as JSON at
    ``<user-data-dir>/claude-code-sessions/<accountUuid>/<uuid>/scheduled-tasks.json``.

The metadata file is an undocumented internal format, so every write here
validates the file's shape first, keeps a backup, and refuses rather than
guessing. A refusal is reported to the caller so the supervisor can surface
``needs user action`` instead of silently continuing.

The app caches this file in memory. Only write while Claude Desktop is stopped;
the rotation sequence already guarantees that.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

TASK_PREFIX = "omni-route-rotation"
ROTATION_CRON = "*/5 * * * *"
SESSIONS_DIRNAME = "claude-code-sessions"
STORE_NAME = "scheduled-tasks.json"


def task_id_for(workspace: Path) -> str:
    """A task id unique to one workspace.

    The automation is identified by (account, workspace): the record lives in
    the account's store, but `cwd` and the prompt are per workspace. Without the
    workspace in the id, two projects would share one SKILL.md and overwrite
    each other's prompt.
    """
    resolved = str(Path(workspace).resolve())
    digest = hashlib.sha256(resolved.encode("utf-8")).hexdigest()[:8]
    return f"{TASK_PREFIX}-{digest}"


def _app_owns_store(user_data_dir: Path) -> bool:
    """True when Claude Desktop is running under this profile."""
    # No leading "--": pgrep reads that as an option terminator.
    result = subprocess.run(
        ["pgrep", "-f", f"user-data-dir={Path(user_data_dir)}"],
        capture_output=True, text=True,
    )
    return result.returncode == 0


class SchedulerError(RuntimeError):
    """The task store is missing or does not match the expected shape."""


@dataclass(frozen=True)
class TaskStore:
    path: Path
    account_uuid: str


def rotation_prompt(workspace: Path) -> str:
    """The self-gating prompt. Harmless when there is nothing to do."""
    # Resolve so the path in the prompt matches the record's cwd exactly; the
    # agent must look for the marker where the supervisor writes it.
    root = Path(workspace).resolve()
    marker = root / ".omni-route" / "handoff-pending"
    inflight = root / ".omni-route" / "handoff-inflight"
    latest = root / ".omni-route" / "handoff-latest.md"
    return f"""First, check which of these two files exists:

* `{marker}`
* `{inflight}`

If NEITHER exists, stop immediately and reply with exactly: no pending handoff.
Do not investigate, do not read other files, do not start any work.

If `{inflight}` exists, another run is already working on this handoff. Stop
immediately and reply with exactly: handoff already in progress.

If `{marker}` exists, an account rotation just happened and you are continuing
work another agent started. Do these steps in this exact order:

1. FIRST rename the marker so no other run picks up the same handoff:
     mv '{marker}' '{inflight}'
2. Read `{latest}`.
3. Verify the repository state yourself with `git status` and `git log`. The
   repository is the source of truth; the handoff is only a pointer.
4. Carry out the "Exact next action" from the handoff, and keep going until it
   is genuinely done.
5. ONLY when the work is complete, delete `{inflight}`.

Step 5 must be last. If you delete the marker before finishing and this session
is interrupted, the handoff is lost and the work is abandoned silently.

Do not restate the task back to the user or ask them to repeat anything. They
should not have to notice that the account changed."""


def trust_workspace(workspace: Path, *, claude_config_dir: Path | None = None) -> bool:
    """Record workspace trust inside an account profile.

    A scheduled task whose folder is untrusted does not fail loudly: the app
    dispatches the session, blocks on `LocalSessions.checkTrust`, and the
    dispatch later expires and is reported as "Skipped". Since Omni Route
    creates both the workspace and the task without a human present, trust must
    be recorded up front or every rotation stalls this way.

    Returns True when the entry had to be added.
    """
    config_path = config_dir(claude_config_dir) / ".claude.json"
    if config_path.exists():
        try:
            data = json.loads(config_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise SchedulerError(f"{config_path} is not valid JSON: {exc}") from exc
        if not isinstance(data, dict):
            raise SchedulerError(f"{config_path} is not a JSON object")
    else:
        config_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        data = {}

    projects = data.setdefault("projects", {})
    if not isinstance(projects, dict):
        raise SchedulerError(f"{config_path} projects is not a JSON object")
    key = str(Path(workspace).resolve())
    project = projects.setdefault(key, {})
    if not isinstance(project, dict):
        raise SchedulerError(f"{config_path} project entry is not a JSON object")

    if data.get("hasCompletedOnboarding") is True and project.get(
        "hasTrustDialogAccepted"
    ) is True:
        return False

    data["hasCompletedOnboarding"] = True
    project["hasTrustDialogAccepted"] = True
    temporary = config_path.with_name(f".{config_path.name}.omni-route-tmp")
    temporary.write_text(json.dumps(data, separators=(",", ":")), encoding="utf-8")
    temporary.replace(config_path)
    return True


# Tools the rotation continuation needs before it can do anything useful.
# bypassPermissions is deliberately not used: it is opt-in per account
# (bypassPermissionsOptInByAccount in the app), and when an account has not
# opted in the app silently downgrades the task to "default", which then stalls
# on its first tool call with nobody present to approve it.
UNATTENDED_ALLOW = ["Bash", "Read", "Write", "Edit", "Glob", "Grep"]


def ensure_unattended_settings(
    claude_config_dir: Path | None = None, *, allow: list[str] | None = None
) -> bool:
    """Grant a profile the permissions a scheduled session needs.

    Allow rules in the profile's settings.json apply to scheduled-task sessions,
    so this is the file-based equivalent of answering permission prompts that
    nobody is there to answer. Returns True when the file had to change.
    """
    settings_path = config_dir(claude_config_dir) / "settings.json"
    wanted = list(allow if allow is not None else UNATTENDED_ALLOW)

    if settings_path.exists():
        try:
            data = json.loads(settings_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise SchedulerError(f"{settings_path} is not valid JSON: {exc}") from exc
        if not isinstance(data, dict):
            raise SchedulerError(f"{settings_path} is not a JSON object")
    else:
        settings_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        data = {}

    permissions = data.setdefault("permissions", {})
    if not isinstance(permissions, dict):
        raise SchedulerError(f"{settings_path} permissions is not a JSON object")
    existing = permissions.get("allow")
    existing = list(existing) if isinstance(existing, list) else []

    missing = [rule for rule in wanted if rule not in existing]
    mode_ok = permissions.get("defaultMode") == "acceptEdits"
    if not missing and mode_ok:
        return False

    permissions["allow"] = existing + missing
    permissions["defaultMode"] = "acceptEdits"
    temporary = settings_path.with_name(f".{settings_path.name}.omni-route-tmp")
    temporary.write_text(json.dumps(data, indent=2), encoding="utf-8")
    temporary.replace(settings_path)
    return True


def config_dir(explicit: Path | None = None) -> Path:
    return Path(explicit) if explicit else Path.home() / ".claude"


def skill_path(task_id: str, *, claude_config_dir: Path | None = None) -> Path:
    return config_dir(claude_config_dir) / "scheduled-tasks" / task_id / "SKILL.md"


def write_skill(
    prompt: str,
    *,
    task_id: str,
    description: str = "Omni Route rotation continuation",
    claude_config_dir: Path | None = None,
) -> Path:
    path = skill_path(task_id, claude_config_dir=claude_config_dir)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    body = f"---\nname: {task_id}\ndescription: {description}\n---\n\n{prompt}\n"
    path.write_text(body, encoding="utf-8")
    return path


def find_store(user_data_dir: Path, account_uuid: str | None = None) -> TaskStore:
    """Locate the scheduled-task metadata file for an account.

    Raises SchedulerError when no existing store is found. A store is never
    created from nothing: the app owns that directory layout, and inventing one
    is exactly the kind of guess that should surface as `needs user action`.
    """
    sessions = Path(user_data_dir) / SESSIONS_DIRNAME
    if not sessions.is_dir():
        raise SchedulerError(f"no {SESSIONS_DIRNAME} directory under {user_data_dir}")
    roots = [sessions / account_uuid] if account_uuid else sorted(
        p for p in sessions.iterdir() if p.is_dir()
    )
    candidates: list[TaskStore] = []
    for root in roots:
        if not root.is_dir():
            continue
        for store in sorted(root.glob(f"*/{STORE_NAME}")):
            candidates.append(TaskStore(path=store, account_uuid=root.name))
    if not candidates:
        raise SchedulerError(
            f"no {STORE_NAME} under {sessions}"
            f"{' for account ' + account_uuid if account_uuid else ''};"
            " open Claude Desktop once and create any scheduled task"
        )
    # Prefer the most recently modified store when an account has several.
    candidates.sort(key=lambda c: c.path.stat().st_mtime, reverse=True)
    return candidates[0]


def load_store(store: TaskStore) -> dict[str, Any]:
    try:
        data = json.loads(store.path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SchedulerError(f"{store.path} is unreadable: {exc}") from exc
    if not isinstance(data, dict):
        raise SchedulerError(f"{store.path} is not a JSON object")
    tasks = data.get("scheduledTasks")
    if not isinstance(tasks, list):
        raise SchedulerError(f"{store.path} has no scheduledTasks list")
    for entry in tasks:
        if not isinstance(entry, dict) or "id" not in entry:
            raise SchedulerError(f"{store.path} contains an unexpected task record")
    return data


def build_record(
    *,
    task_id: str,
    skill_file: Path,
    workspace: Path,
    cron: str | None = ROTATION_CRON,
    permission_mode: str = "acceptEdits",
    display_name: str = "Omni Route rotation",
) -> dict[str, Any]:
    record: dict[str, Any] = {
        "id": task_id,
        "displayName": display_name,
        "enabled": True,
        "filePath": str(skill_file),
        "createdAt": int(time.time() * 1000),
        "cwd": str(Path(workspace).resolve()),
        # A worktree would place the continuation in a new tree, away from the
        # branch the outgoing agent committed to. Must stay false.
        "useWorktree": False,
        # The app otherwise staggers runs by minutes, which is latency on every
        # rotation.
        "disableJitter": True,
        "permissionMode": permission_mode,
    }
    if cron:
        record["cronExpression"] = cron
    return record


def install(
    user_data_dir: Path,
    workspace: Path,
    *,
    account_uuid: str | None = None,
    claude_config_dir: Path | None = None,
    task_id: str | None = None,
    cron: str | None = ROTATION_CRON,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Install or update the rotation automation. Returns a result summary."""
    task_id = task_id or task_id_for(workspace)
    if not dry_run and _app_owns_store(user_data_dir):
        raise SchedulerError(
            f"Claude Desktop is running under {user_data_dir}; stop it before "
            "writing its task store. The app holds the task list in memory and "
            "writes it back, so a write made now is silently discarded."
        )
    if not dry_run:
        # Untrusted folders make scheduled sessions stall and report "Skipped".
        trust_workspace(workspace, claude_config_dir=claude_config_dir)
        # A session with no permissions stalls on its first tool call.
        ensure_unattended_settings(claude_config_dir)
    store = find_store(Path(user_data_dir), account_uuid)
    data = load_store(store)
    skill_file = write_skill(
        rotation_prompt(Path(workspace)),
        task_id=task_id,
        claude_config_dir=claude_config_dir,
    ) if not dry_run else skill_path(task_id, claude_config_dir=claude_config_dir)

    record = build_record(
        task_id=task_id, skill_file=skill_file, workspace=Path(workspace), cron=cron
    )
    tasks: list[dict[str, Any]] = data["scheduledTasks"]
    existing = next((t for t in tasks if t.get("id") == task_id), None)
    action = "updated" if existing else "created"
    if existing is not None:
        # Preserve createdAt so the app does not treat it as a brand new task.
        record["createdAt"] = existing.get("createdAt", record["createdAt"])
        tasks[[t.get("id") for t in tasks].index(task_id)] = {**existing, **record}
    else:
        tasks.append(record)

    if dry_run:
        return {"action": f"would be {action}", "store": str(store.path), "record": record}

    backup = store.path.with_suffix(f".json.omni-route-backup-{int(time.time())}")
    shutil.copy2(store.path, backup)
    temporary = store.path.with_suffix(".json.omni-route-tmp")
    temporary.write_text(json.dumps(data, indent=2), encoding="utf-8")
    temporary.replace(store.path)
    return {
        "action": action,
        "store": str(store.path),
        "backup": str(backup),
        "account_uuid": store.account_uuid,
        "skill": str(skill_file),
        "record": record,
    }


def remove(
    user_data_dir: Path,
    *,
    account_uuid: str | None = None,
    task_id: str | None = None,
    workspace: Path | None = None,
) -> bool:
    if task_id is None:
        if workspace is None:
            raise SchedulerError("remove() needs either task_id or workspace")
        task_id = task_id_for(workspace)
    store = find_store(Path(user_data_dir), account_uuid)
    data = load_store(store)
    tasks = data["scheduledTasks"]
    remaining = [t for t in tasks if t.get("id") != task_id]
    if len(remaining) == len(tasks):
        return False
    data["scheduledTasks"] = remaining
    backup = store.path.with_suffix(f".json.omni-route-backup-{int(time.time())}")
    shutil.copy2(store.path, backup)
    temporary = store.path.with_suffix(".json.omni-route-tmp")
    temporary.write_text(json.dumps(data, indent=2), encoding="utf-8")
    temporary.replace(store.path)
    return True
