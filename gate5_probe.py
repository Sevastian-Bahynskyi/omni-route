#!/usr/bin/env python3
"""Gate 5 probe: find where Claude Desktop stores scheduled-task metadata.

Usage:
    python3 gate5_probe.py before     # snapshot, then create ONE task in the UI
    python3 gate5_probe.py after      # snapshot again and print what changed

Writes full detail to ~/.omni-route-gate5/ and prints a short report.

Secrets are never printed. Values from .claude.json are shown only for keys
matching the routine/task/schedule family, and any key whose name looks like a
credential is redacted.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any

OUT = Path.home() / ".omni-route-gate5"
SECRET_HINT = ("token", "secret", "key", "oauth", "password", "credential", "auth")
TASK_HINT = ("routine", "task", "schedul", "cron")

WATCH_TREES = [
    Path.home() / ".claude" / "scheduled-tasks",
    Path.home() / ".claude" / "settings.json",
    Path.home() / "Library" / "Application Support" / "Claude" / "Local Storage",
    Path.home() / "Library" / "Application Support" / "Claude" / "IndexedDB",
    Path.home() / "Library" / "Application Support" / "Claude" / "Preferences",
]


def _digest(path: Path) -> str:
    try:
        if path.is_dir():
            return "dir"
        data = path.read_bytes()
        return hashlib.sha256(data).hexdigest()[:16] + f":{len(data)}"
    except OSError as exc:
        return f"unreadable:{exc.errno}"


def _walk(root: Path) -> dict[str, str]:
    found: dict[str, str] = {}
    if not root.exists():
        return found
    if root.is_file():
        found[str(root)] = _digest(root)
        return found
    for path in sorted(root.rglob("*")):
        try:
            found[str(path)] = _digest(path)
        except OSError:
            continue
    return found


def _claude_json() -> dict[str, Any]:
    path = Path.home() / ".claude.json"
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _task_subtree(data: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in data.items():
        lowered = key.lower()
        if any(hint in lowered for hint in SECRET_HINT):
            continue
        if any(hint in lowered for hint in TASK_HINT):
            out[key] = value
    return out


def snapshot() -> dict[str, Any]:
    files: dict[str, str] = {}
    for root in WATCH_TREES:
        files.update(_walk(root))
    data = _claude_json()
    return {
        "files": files,
        "claude_json_keys": sorted(data.keys()),
        "claude_json_tasks": _task_subtree(data),
    }


def main() -> int:
    if len(sys.argv) < 2 or sys.argv[1] not in {"before", "after"}:
        print(__doc__)
        return 2
    phase = sys.argv[1]
    OUT.mkdir(parents=True, exist_ok=True, mode=0o700)
    current = snapshot()
    (OUT / f"{phase}.json").write_text(json.dumps(current, indent=2, default=str), encoding="utf-8")

    if phase == "before":
        print(f"BEFORE snapshot saved. {len(current['files'])} files tracked.")
        print("Now create ONE scheduled task in Claude Desktop, then run: python3 gate5_probe.py after")
        return 0

    prior_path = OUT / "before.json"
    if not prior_path.exists():
        print("ERROR: no before.json. Run `python3 gate5_probe.py before` first.")
        return 1
    prior = json.loads(prior_path.read_text(encoding="utf-8"))

    before_files, after_files = prior["files"], current["files"]
    added = sorted(set(after_files) - set(before_files))
    changed = sorted(k for k in set(after_files) & set(before_files) if after_files[k] != before_files[k])
    new_keys = sorted(set(current["claude_json_keys"]) - set(prior["claude_json_keys"]))

    lines: list[str] = []
    lines.append("=== GATE 5 PROBE RESULT ===")
    lines.append(f"new files ({len(added)}):")
    lines.extend(f"  + {p}" for p in added[:40])
    lines.append(f"changed files ({len(changed)}):")
    lines.extend(f"  ~ {p}" for p in changed[:40])
    lines.append(f"new .claude.json keys: {new_keys or 'none'}")
    lines.append("task-related .claude.json values now:")
    lines.append(json.dumps(current["claude_json_tasks"], indent=2, default=str)[:1500])

    tasks_dir = Path.home() / ".claude" / "scheduled-tasks"
    lines.append(f"scheduled-tasks dir exists: {tasks_dir.exists()}")
    if tasks_dir.exists():
        for skill in sorted(tasks_dir.rglob("SKILL.md")):
            lines.append(f"--- {skill} ---")
            try:
                lines.append(skill.read_text(encoding="utf-8")[:800])
            except OSError as exc:
                lines.append(f"(unreadable: {exc})")
        siblings = sorted(p for p in tasks_dir.rglob("*") if p.is_file() and p.name != "SKILL.md")
        lines.append(f"other files under scheduled-tasks ({len(siblings)}):")
        for path in siblings[:20]:
            lines.append(f"  {path}")
            if path.suffix in {".json", ".yaml", ".yml", ".toml"} and path.stat().st_size < 4000:
                try:
                    lines.append("    " + path.read_text(encoding="utf-8").replace("\n", "\n    ")[:600])
                except OSError:
                    pass

    report = "\n".join(lines)
    (OUT / "report.txt").write_text(report, encoding="utf-8")
    print(report)
    print(f"\nFull detail: {OUT}/report.txt")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
