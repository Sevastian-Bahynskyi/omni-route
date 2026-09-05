#!/usr/bin/env python3
"""Gate 6: prove the rotation automation resumes work unattended.

Runs against an isolated throwaway workspace, never your real project.

    python3 gate6.py setup     # app must be STOPPED
    python3 gate6.py arm       # app must be RUNNING
    python3 gate6.py check
    python3 gate6.py cleanup

Each command prints what to do next.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

import handoff
import native_scheduler as ns

PROFILE = "claude-3"
UDD = Path.home() / ".omnigent" / "claude-desktop" / PROFILE
CFG = Path.home() / ".omnigent" / "claude-accounts" / PROFILE
WORKSPACE = Path.home() / "omni-route-gate6-workspace"
PROOF = "GATE6_PROOF.txt"
TOKEN = "gate6-resume-ok"
APP_BIN = "/Applications/Claude.app/Contents/MacOS/Claude"


def _app_running() -> bool:
    # No leading "--" in the pattern: pgrep would parse it as an option
    # terminator and never match.
    result = subprocess.run(["pgrep", "-f", f"user-data-dir={UDD}"],
                            capture_output=True, text=True)
    return result.returncode == 0


def _die(message: str) -> int:
    print(f"\nSTOP: {message}")
    return 1


def setup() -> int:
    if _app_running():
        return _die(
            f"Claude is running under the {PROFILE} profile. Quit it first "
            "(Cmd+Q), then run this again.\n"
            "The app caches its task list in memory and would overwrite the write."
        )

    if not UDD.is_dir():
        return _die(f"{UDD} does not exist. The {PROFILE} profile is not set up.")

    WORKSPACE.mkdir(parents=True, exist_ok=True)
    if not (WORKSPACE / ".git").is_dir():
        subprocess.run(["git", "init", "-q", str(WORKSPACE)], check=True)
        subprocess.run(["git", "-C", str(WORKSPACE), "config", "user.email",
                        "gate6@example.com"], check=True)
        subprocess.run(["git", "-C", str(WORKSPACE), "config", "user.name", "Gate6"],
                       check=True)
        (WORKSPACE / "README.md").write_text("gate 6 scratch workspace\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(WORKSPACE), "add", "-A"], check=True)
        subprocess.run(["git", "-C", str(WORKSPACE), "commit", "-qm", "init"], check=True)

    try:
        result = ns.install(UDD, WORKSPACE, claude_config_dir=CFG)
    except ns.SchedulerError as exc:
        return _die(f"could not install the automation: {exc}")

    print("Automation installed.")
    print(f"  workspace : {WORKSPACE}")
    print(f"  task id   : {result['record']['id']}")
    print(f"  account   : {result['account_uuid']}")
    print(f"  store     : {result['store']}")
    print(f"  backup    : {result['backup']}")
    print("\nNEXT:")
    print("  1. Start the profile:")
    print(f"       ./launch_claude_profile.sh {PROFILE} --with-config-dir")
    print("  2. Leave that window open. Then run:")
    print("       python3 gate6.py arm")
    return 0


def arm() -> int:
    if not WORKSPACE.is_dir():
        return _die("run `python3 gate6.py setup` first.")
    if not _app_running():
        print("WARNING: Claude does not appear to be running under the "
              f"{PROFILE} profile. Scheduled tasks only fire while the app is open.")

    proof = WORKSPACE / PROOF
    if proof.exists():
        proof.unlink()

    record = handoff.Handoff(
        goal="Prove that a rotation resumes automatically.",
        progress="The previous account reached its quota threshold and stopped.",
        decisions="None needed; this is a mechanism test.",
        files=PROOF,
        status="No tests to run.",
        blockers="none",
        next_action=(
            f"Create a file named {PROOF} in this workspace whose entire "
            f"contents are exactly: {TOKEN}\nThen stop. Do nothing else."
        ),
        from_account="claude-1",
        to_account=PROFILE,
    )
    path = handoff.write(WORKSPACE, record)
    print(f"Handoff armed: {path}")
    print(f"Pending marker: {WORKSPACE / '.omni-route' / 'handoff-pending'}")
    print("\nNEXT:")
    print("  Wait up to 6 minutes (the automation ticks every 5), then run:")
    print("       python3 gate6.py check")
    return 0


def check() -> int:
    proof = WORKSPACE / PROOF
    pending = handoff.is_pending(WORKSPACE)
    inflight = handoff.is_inflight(WORKSPACE)
    print(f"proof file present : {proof.exists()}")
    print(f"handoff still armed: {pending}")
    print(f"handoff in flight  : {inflight}")

    if proof.exists():
        contents = proof.read_text(encoding="utf-8").strip()
        print(f"proof contents     : {contents!r}")
        if contents == TOKEN and not pending and not inflight:
            print("\nGATE 6 PASS — the automation resumed the task unattended,")
            print("did the work, and cleared the marker.")
            return 0
        if contents == TOKEN:
            print("\nPARTIAL — the work was done but the marker was not cleared.")
            print("The automation would re-run on the same handoff. Needs a fix.")
            return 1
        print("\nFAIL — the proof file has unexpected contents.")
        return 1

    if inflight:
        print("\nA run claimed the handoff and has not finished it yet.")
    elif not pending:
        print("\nWARNING: no handoff is armed and no proof exists. The marker was")
        print("consumed without the work being done - run `arm` again.")
    print("\nNot yet. Either it has not fired, or it did not run.")
    print("Check the Claude window: a session should appear under 'Scheduled'.")
    print("If nothing appears after ~7 minutes, run:")
    print("       python3 gate6.py diagnose")
    return 1


def diagnose() -> int:
    print(f"app running under {PROFILE}: {_app_running()}")
    print(f"workspace exists          : {WORKSPACE.is_dir()}")
    print(f"handoff armed             : {handoff.is_pending(WORKSPACE)}")
    try:
        store = ns.find_store(UDD)
        data = ns.load_store(store)
        print(f"task store                : {store.path}")
        for task in data["scheduledTasks"]:
            print(f"  - {task.get('id')}: cron={task.get('cronExpression')!r} "
                  f"enabled={task.get('enabled')} cwd={task.get('cwd')}")
            skill = Path(task.get("filePath", ""))
            print(f"    prompt file exists: {skill.exists()}")
    except ns.SchedulerError as exc:
        print(f"task store                : ERROR {exc}")
    return 0


def cleanup() -> int:
    if _app_running():
        return _die("quit Claude under this profile first, then run cleanup again.")
    try:
        removed = ns.remove(UDD, workspace=WORKSPACE)
        print(f"automation removed: {removed}")
    except ns.SchedulerError as exc:
        print(f"automation removal skipped: {exc}")
    if WORKSPACE.is_dir():
        shutil.rmtree(WORKSPACE)
        print(f"workspace removed : {WORKSPACE}")
    skill_dir = ns.skill_path(ns.task_id_for(WORKSPACE), claude_config_dir=CFG).parent
    if skill_dir.is_dir():
        shutil.rmtree(skill_dir)
        print(f"prompt removed    : {skill_dir}")
    print("\nDone. The omni-route-probe routines are untouched.")
    return 0


COMMANDS = {"setup": setup, "arm": arm, "check": check,
            "diagnose": diagnose, "cleanup": cleanup}


def main() -> int:
    if len(sys.argv) < 2 or sys.argv[1] not in COMMANDS:
        print(__doc__)
        return 2
    return COMMANDS[sys.argv[1]]()


if __name__ == "__main__":
    raise SystemExit(main())
