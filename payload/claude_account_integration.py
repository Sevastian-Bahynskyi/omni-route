from __future__ import annotations

import filecmp
import shutil
import uuid
from pathlib import Path

from omnigent.codex_account_pool import AccountProfile, read_runtime, request_rotation

_AUTH_ENV = (
    "ANTHROPIC_PROFILE", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL",
    "CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN_FILE_DESCRIPTOR",
    "CLAUDE_CODE_API_KEY_FILE_DESCRIPTOR", "CLAUDE_SECURESTORAGE_CONFIG_DIR",
    "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY",
    "CLAUDE_CONFIG_DIR",
)


def prepare_account_environment(
    profile: AccountProfile, launch_env: dict[str, str],
) -> tuple[dict[str, str], list[str]]:
    if profile.provider != "claude" or profile.config_dir is None:
        raise RuntimeError("Selected subscription account requires a different provider.")
    env = {key: value for key, value in launch_env.items() if key not in _AUTH_ENV}
    unset = list(_AUTH_ENV)
    if not profile.use_default_config:
        config_dir = profile.config_dir
        config_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        projects = Path.home() / ".claude" / "projects"
        projects.mkdir(parents=True, exist_ok=True, mode=0o700)
        account_projects = config_dir / "projects"
        if account_projects.resolve() == projects.resolve():
            env["CLAUDE_CONFIG_DIR"] = str(config_dir)
            unset.remove("CLAUDE_CONFIG_DIR")
            return env, unset
        if account_projects.is_dir() and not account_projects.is_symlink():
            if next(account_projects.iterdir(), None) is not None:
                _merge_history(account_projects, projects)
                account_projects.rename(config_dir / f"projects.before-pool-{uuid.uuid4().hex}")
            else:
                account_projects.rmdir()
        if not account_projects.exists() and not account_projects.is_symlink():
            account_projects.symlink_to(projects, target_is_directory=True)
        if account_projects.resolve() != projects.resolve():
            raise RuntimeError("Claude account history directory does not match shared sessions.")
        env["CLAUDE_CONFIG_DIR"] = str(config_dir)
        unset.remove("CLAUDE_CONFIG_DIR")
    return env, unset


def _merge_history(source: Path, destination: Path) -> None:
    entries = list(source.rglob("*"))
    for entry in entries:
        target = destination / entry.relative_to(source)
        if entry.is_symlink() or (target.exists() and entry.is_dir() != target.is_dir()):
            raise RuntimeError("Claude history contains conflicting paths; existing history was preserved.")
        if entry.is_file() and target.exists() and not filecmp.cmp(entry, target, shallow=False):
            raise RuntimeError("Claude history contains conflicting sessions; existing history was preserved.")
    for entry in entries:
        target = destination / entry.relative_to(source)
        if entry.is_dir():
            target.mkdir(parents=True, exist_ok=True, mode=0o700)
        elif entry.is_file() and not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            with target.open("xb") as handle, entry.open("rb") as original:
                shutil.copyfileobj(original, handle)
            target.chmod(0o600)


def request_claude_rotation(session_id: str) -> bool:
    from omnigent.codex_native_bridge import bridge_dir_for_bridge_id

    bridge_dir = bridge_dir_for_bridge_id(session_id)
    runtime = read_runtime(bridge_dir)
    if runtime is None or runtime.get("mode") != "claude":
        return False
    account_name = runtime.get("account_name")
    if not isinstance(account_name, str):
        return False
    request_rotation(bridge_dir, session_id=session_id, account_name=account_name,
                     retry_at=None, reason="usage_limit_exceeded", replay_required=True)
    return True
