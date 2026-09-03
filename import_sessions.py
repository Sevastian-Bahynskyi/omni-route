#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

BASE_URL = "http://127.0.0.1:6767"
HOME = Path.home()
POOL_CONFIG = HOME / ".omnigent" / "codex-account-pool.json"
PATCHED_BASE = HOME / ".local" / "share" / "omnigent-subscription-rotation"
PATCHED_OMNI = PATCHED_BASE / "omnigent" / ".venv" / "bin" / "omni"
LOG_PATH = PATCHED_BASE / "session-import-server.log"


@dataclass(frozen=True)
class Candidate:
    source: str
    session_id: str
    home: Path
    transcript: Path
    mtime: float
    size: int


def read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _dedupe_paths(paths: list[Path]) -> list[Path]:
    seen: set[str] = set()
    result: list[Path] = []
    for path in paths:
        key = os.path.normpath(str(path.expanduser()))
        if key in seen:
            continue
        seen.add(key)
        result.append(Path(key))
    return result


def codex_homes() -> list[Path]:
    homes = [HOME / ".codex"]
    env_home = os.environ.get("CODEX_HOME")
    if env_home:
        homes.append(Path(env_home))
    config = read_json(POOL_CONFIG)
    accounts = config.get("accounts")
    if isinstance(accounts, list):
        for item in accounts:
            if not isinstance(item, dict):
                continue
            auth = item.get("auth_json")
            if isinstance(auth, str) and auth:
                homes.append(Path(auth).expanduser().parent)
    return _dedupe_paths(homes)


def claude_homes() -> list[Path]:
    homes = [HOME / ".claude"]
    env_home = os.environ.get("CLAUDE_CONFIG_DIR")
    if env_home:
        homes.append(Path(env_home))
    config = read_json(POOL_CONFIG)
    for account in config.get("accounts", []):
        if isinstance(account, dict) and account.get("provider") == "claude" and isinstance(account.get("config_dir"), str):
            homes.append(Path(account["config_dir"]).expanduser())
    future_root = HOME / ".omnigent" / "claude-accounts"
    if future_root.is_dir():
        homes.extend(path for path in future_root.iterdir() if path.is_dir())
    return _dedupe_paths(homes)


def discover_candidates() -> tuple[list[list[Candidate]], int]:
    groups: dict[tuple[str, str], list[Candidate]] = {}

    for home in codex_homes():
        paths: list[Path] = []
        sessions = home / "sessions"
        archived = home / "archived_sessions"
        if sessions.is_dir():
            paths.extend(p for p in sessions.glob("**/rollout-*.jsonl") if p.is_file())
        if archived.is_dir():
            paths.extend(p for p in archived.glob("rollout-*.jsonl") if p.is_file())
        for path in paths:
            session_id = path.stem[-36:]
            if len(session_id) != 36:
                continue
            try:
                stat = path.stat()
            except OSError:
                continue
            cand = Candidate("codex", session_id, home, path, stat.st_mtime, stat.st_size)
            groups.setdefault((cand.source, cand.session_id), []).append(cand)

    for home in claude_homes():
        root = home / "projects"
        if not root.is_dir():
            continue
        for path in root.rglob("*.jsonl"):
            if not path.is_file() or "subagents" in path.parts:
                continue
            session_id = path.stem
            if not session_id:
                continue
            try:
                stat = path.stat()
            except OSError:
                continue
            cand = Candidate("claude", session_id, home, path, stat.st_mtime, stat.st_size)
            groups.setdefault((cand.source, cand.session_id), []).append(cand)

    duplicate_copies = sum(max(0, len(group) - 1) for group in groups.values())
    ordered = sorted(
        groups.values(),
        key=lambda group: max((c.mtime, c.size) for c in group),
    )
    return ordered, duplicate_copies


def normalize_workspace(value: str | None) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    return os.path.normpath(os.path.expanduser(value.strip()))


def project_name(workspace: str, used: dict[str, str]) -> str:
    path = Path(workspace)
    base = path.name or "workspace"
    if base not in used or used[base] == workspace:
        return base
    parent = path.parent.name
    candidate = f"{parent}/{base}" if parent else base
    if candidate not in used or used[candidate] == workspace:
        return candidate
    digest = hashlib.sha256(workspace.encode()).hexdigest()[:6]
    return f"{base} [{digest}]"


async def ensure_server() -> None:
    from omnigent.cli_auth import open_server_client

    async def healthy() -> bool:
        try:
            async with open_server_client(BASE_URL) as client:
                response = await client.get("/v1/projects", timeout=3.0)
                return response.status_code < 400
        except Exception:
            return False

    if await healthy():
        return
    if not PATCHED_OMNI.is_file():
        raise RuntimeError(f"patched Omnigent executable missing: {PATCHED_OMNI}")
    PATCHED_BASE.mkdir(parents=True, exist_ok=True)
    log = LOG_PATH.open("ab")
    subprocess.Popen(
        [str(PATCHED_OMNI), "server", "--host", "127.0.0.1", "--port", "6767"],
        stdin=subprocess.DEVNULL,
        stdout=log,
        stderr=subprocess.STDOUT,
        start_new_session=True,
        close_fds=True,
    )
    deadline = time.monotonic() + 25.0
    while time.monotonic() < deadline:
        if await healthy():
            return
        await asyncio.sleep(0.5)
    raise RuntimeError(f"Omnigent server did not become ready; see {LOG_PATH}")


def item_payload(item: Any) -> dict[str, Any]:
    value = item.model_dump(mode="json")
    if not isinstance(value, dict):
        raise TypeError("normalized session item did not serialize to an object")
    return value


async def run_import() -> int:
    from omnigent.cli_auth import open_server_client
    from omnigent.session_import.local import load_claude_session, load_codex_session

    candidate_groups, duplicate_copies = discover_candidates()
    print("OMNI ROUTE // SESSION IMPORT")
    print(f"Discovered unique sessions: {len(candidate_groups)}")
    print(f"Shared/duplicate transcript copies collapsed: {duplicate_copies}")
    if not candidate_groups:
        print("No Codex or Claude history found.")
        return 0

    await ensure_server()

    imported = already = failed = projects_created = 0
    async with open_server_client(BASE_URL) as client:
        response = await client.get("/v1/projects", timeout=10.0)
        response.raise_for_status()
        payload = response.json()
        project_rows = payload.get("data", []) if isinstance(payload, dict) else []
        workspace_to_project: dict[str, str] = {}
        used_names: dict[str, str] = {}
        for row in project_rows:
            if not isinstance(row, dict):
                continue
            name = row.get("name")
            project_id = row.get("id")
            config = row.get("config")
            if isinstance(name, str):
                workspace_value = normalize_workspace(config.get("workspace")) if isinstance(config, dict) else None
                used_names[name] = workspace_value or ""
                if workspace_value and isinstance(project_id, str):
                    workspace_to_project[workspace_value] = project_id

        for index, group in enumerate(candidate_groups, 1):
            candidate = max(group, key=lambda c: (c.mtime, c.size))
            try:
                loaded: list[tuple[int, float, int, Candidate, Any]] = []
                for copy in group:
                    try:
                        local_copy = (
                            load_codex_session(copy.session_id, codex_home=copy.home)
                            if copy.source == "codex"
                            else load_claude_session(copy.session_id, claude_home=copy.home)
                        )
                        loaded.append((len(local_copy.items), copy.mtime, copy.size, copy, local_copy))
                    except Exception:
                        continue
                if not loaded:
                    raise RuntimeError("no transcript copy could be normalized")
                _, _, _, candidate, local = max(loaded, key=lambda entry: entry[:3])
                if not local.items:
                    print(f"[{index}/{len(candidate_groups)}] SKIP {candidate.source}:{candidate.session_id} (empty)")
                    continue
                workspace = normalize_workspace(local.workspace)
                project_id: str | None = None
                if workspace:
                    project_id = workspace_to_project.get(workspace)
                    if project_id is None:
                        name = project_name(workspace, used_names)
                        created = await client.post(
                            "/v1/projects",
                            json={"name": name, "config": {"workspace": workspace}},
                            timeout=15.0,
                        )
                        if created.status_code == 409:
                            # A concurrent/manual project creation may have raced us.
                            refreshed = await client.get("/v1/projects", timeout=10.0)
                            refreshed.raise_for_status()
                            rows = refreshed.json().get("data", [])
                            match = next(
                                (
                                    row
                                    for row in rows
                                    if isinstance(row, dict)
                                    and isinstance(row.get("config"), dict)
                                    and normalize_workspace(row["config"].get("workspace")) == workspace
                                ),
                                None,
                            )
                            if match is None or not isinstance(match.get("id"), str):
                                raise RuntimeError(f"project name collision for workspace {workspace}")
                            project_id = match["id"]
                        else:
                            created.raise_for_status()
                            project = created.json()
                            project_id = project.get("id")
                            if not isinstance(project_id, str):
                                raise RuntimeError("project create response had no id")
                            projects_created += 1
                        workspace_to_project[workspace] = project_id
                        used_names[name] = workspace

                body = {
                    "source": local.source,
                    "external_session_id": local.external_session_id,
                    "workspace": local.workspace,
                    "title": local.title,
                    "force": False,
                    "project_id": project_id,
                    "items": [item_payload(item) for item in local.items],
                }
                result = await client.post("/v1/imports", json=body, timeout=120.0)
                if result.status_code == 409:
                    already += 1
                    print(f"[{index}/{len(candidate_groups)}] EXISTING {candidate.source}:{candidate.session_id}")
                    continue
                result.raise_for_status()
                imported += 1
                title = local.title or candidate.session_id
                project_text = f" -> {Path(workspace).name}" if workspace else ""
                copies_text = f" // merged {len(group)} copies" if len(group) > 1 else ""
                print(f"[{index}/{len(candidate_groups)}] IMPORTED {candidate.source}: {title}{project_text}{copies_text}")
            except Exception as exc:  # noqa: BLE001
                failed += 1
                print(f"[{index}/{len(candidate_groups)}] FAILED {candidate.source}:{candidate.session_id} :: {exc}")

    print()
    print("=== SESSION IMPORT SUMMARY ===")
    print(f"Imported: {imported}")
    print(f"Already imported: {already}")
    print(f"Failed: {failed}")
    print(f"Projects created: {projects_created}")
    print(f"Duplicate/shared copies collapsed: {duplicate_copies}")
    # Historical one-off malformed transcripts should not make installation unusable.
    return 0


def self_test() -> int:
    import tempfile

    assert normalize_workspace("~/x/../x/repo").endswith("/x/repo")
    used = {"repo": "/a/repo"}
    assert project_name("/b/repo", used) != "repo"
    a = Candidate("codex", "s", Path("/a"), Path("/a/x"), 1, 10)
    b = Candidate("codex", "s", Path("/b"), Path("/b/x"), 2, 5)
    assert max((a, b), key=lambda c: (c.mtime, c.size)) is b

    # The same Codex thread can exist under multiple account homes after being
    # resumed with another subscription. Discovery must collapse those copies
    # into one source/session group rather than importing duplicate conversations.
    global HOME, POOL_CONFIG
    old_home, old_pool = HOME, POOL_CONFIG
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        HOME = root
        POOL_CONFIG = root / ".omnigent" / "codex-account-pool.json"
        thread = "12345678-1234-1234-1234-123456789abc"
        homes = [root / ".codex", root / ".omnigent" / "codex-accounts" / "codex-2"]
        for i, home in enumerate(homes, start=1):
            rollout = home / "sessions" / "2026" / "09" / "03" / f"rollout-test-{thread}.jsonl"
            rollout.parent.mkdir(parents=True, exist_ok=True)
            rollout.write_text("{}\n" * i, encoding="utf-8")
        POOL_CONFIG.parent.mkdir(parents=True, exist_ok=True)
        POOL_CONFIG.write_text(
            json.dumps({
                "accounts": [
                    {"name": "codex-2", "auth_json": str(homes[1] / "auth.json")}
                ]
            }),
            encoding="utf-8",
        )
        groups, duplicate_copies = discover_candidates()
        codex_groups = [g for g in groups if g and g[0].source == "codex"]
        assert len(codex_groups) == 1
        assert len(codex_groups[0]) == 2
        assert duplicate_copies == 1
    HOME, POOL_CONFIG = old_home, old_pool

    print("session-import self-test: PASS")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Import Codex and Claude session history into Omnigent projects")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        return self_test()
    try:
        return asyncio.run(run_import())
    except Exception as exc:  # noqa: BLE001
        print(f"SESSION IMPORT FAILED: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
