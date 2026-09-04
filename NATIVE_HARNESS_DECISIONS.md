# Native Harness — Recorded Product Decisions

Output of the `/grill-me` pass required by `NATIVE_HARNESS_DIRECTION.md` step 1.
These answers are settled and supersede any conflicting reading of the direction
document. Where a decision changes the direction document, the change is called
out explicitly under "Amendments".

Recorded: 2026-09-04.

---

## 1. Execution surface

Both providers run as **native desktop applications**. Codex Desktop for Codex,
Claude Desktop (Code tab) for Claude. The CLI is not part of the normal path.

Omni Route never renders conversation UI. It manages accounts, quota, rotation,
restart/recovery and the routing dashboard.

## 2. Account isolation

**Codex** — one shared `CODEX_HOME`. Rotation hot-swaps `auth.json` inside it.
Sessions and rollouts never move. Codex Desktop resolves its account from
`CODEX_HOME/auth.json`, so the desktop app follows the swap.

**Claude** — one `CLAUDE_CONFIG_DIR` per account, reusing the existing
`~/.omnigent/claude-accounts/claude-N` profiles and their per-profile Keychain
entries. Fallback if Claude Desktop ignores `CLAUDE_CONFIG_DIR`: a per-account
Electron `--user-data-dir`.

**Concurrency is forbidden.** One account at a time per provider. A request to
run two sessions of the same provider on two accounts is rejected, not queued.

## 3. Thresholds

- Switch threshold is dashboard-configurable, hard maximum **95%**.
- Preparation threshold is always `switch - 3` percentage points.
- The **5-hour session window is the primary signal**. The weekly window also
  trips the threshold, as a backstop against being stranded mid-week.
- Sampling: poll every 60-90s, plus immediate reaction to a hard-limit error.
- The last good reading is cached. A failed read means *unknown* and never
  triggers a rotation. No sampling while a rotation is in flight.

## 4. Rotation — one mechanism for both same-provider and cross-provider

1. **Preparation threshold.** The agent is told to wrap up: finish the current
   safe unit, start no long work. It commits work in progress (commit, not push)
   and writes the handoff.
2. **Switch threshold.** Rotation is armed. It fires at the next turn boundary,
   detected by the session's `Stop` hook. Hard cap of 10 minutes, after which the
   restart is forced and recorded as *unclean*.
3. **Swap.** The supervisor quits the desktop app, swaps the account
   (Codex: `auth.json`; Claude: `CLAUDE_CONFIG_DIR`), relaunches.
4. **Resume.** A self-gating recurring automation already registered in that app
   fires and continues the task in a fresh session.
5. **Verify**, then mark the rotation successful.

Same-provider rotation therefore preserves **the task**, not the transcript.
See Amendments.

## 5. Handoff

Written by the **outgoing agent at the preparation threshold**, while quota
headroom still exists — never at the switch threshold.

Location: `.omni-route/handoff-<timestamp>.md` inside the workspace, git-ignored,
plus `handoff-latest.md` and a `handoff-pending` marker. Per-workspace, so
multiple projects cannot collide.

Contents: the seven fields from the direction document, **plus `worktree_path`
and `branch`**. Those two are new and are required because Claude Desktop places
each session in its own git worktree; without them the next provider cannot find
the work.

Delivery is **by pointer, not by paste**. The incoming agent is told to read the
handoff and verify git state itself. The repository remains the source of truth.

## 6. Self-gating automation

Each desktop app has a recurring (~5 minute) automation registered whose prompt
is self-gating:

> If `.omni-route/handoff-pending` exists, consume it and continue that task.
> Otherwise stop immediately.

This makes repeat firings harmless and removes any dependence on missed-run
catch-up, which Claude Desktop has and Codex Desktop does not
(openai/codex#24327).

## 7. Verification and failure handling

- **Pre-flight:** read the account identity from the credential store before
  launching. Catches a swap that did not take.
- **Confirmation:** the session's first hook fire proves a real turn started on
  the expected account. A session that exists but never starts a turn is a known
  Codex Desktop failure (openai/codex#19969) and must not read as success.
- **If no heartbeat arrives:** retry the restart twice. If a turn still does not
  start, run exactly one continuation turn headlessly (`codex exec` /
  `claude -p`), then hand control straight back to Desktop. This is a fenced
  exception to the desktop-only rule, is limited to a single turn, and is
  surfaced in the dashboard as a **degraded** rotation, never as a clean one.
- **If the account cannot be confirmed at all:** hard stop in `needs user
  action`. No retry loop.
- Rotation is serialized by lockfile. Manual and automatic switches cannot race.
- Exhausted pool, or login/approval required, stops visibly instead of looping.

## 8. Supervisor and state

- The three `payload/` modules are vendored into Omni Route as standalone,
  Omnigent-free modules, keeping their tested rotation, cooldown and
  serialization logic. They are not rewritten from scratch.
- State stays at `~/.omnigent/codex-account-pool.json` and
  `-state.json`. Existing accounts, cooldowns and bindings carry over untouched.
- The supervisor is a foreground process owned by `omni-route start`, guarded by
  a lockfile, and exits when no controlled work remains.
- The **dashboard** is the always-on component, under launchd.

## 9. Omnigent removal

Removed: `apply_patch.py` and the pinned checkout; `import_sessions.py`.
Reimplemented: the Codex quota read, as a direct app-server JSON-RPC client.
Reduced: `remote_access.py` to dashboard-only Tailscale — the 6767 proxy for the
Omnigent phone app dies, since `codex remote-control pair` and
`claude --remote-control` become the remote path.
Rewritten against the vendored modules: `diagnose.py`, `self_test.py`,
`test_routing.py`, `test_integration.py`.

Known loss: no unified cross-provider history view replaces `import_sessions.py`.

---

## Amendments to NATIVE_HARNESS_DIRECTION.md

**Acceptance criteria 3 and 4** previously read "restarts and resumes the exact
native Codex/Claude session automatically". Neither desktop app supports opening
a specific session by ID from outside:

- Codex Desktop has no stable deep link or app-server method for it
  (openai/codex#21779, open).
- Claude Desktop's documented equivalent of `--resume` is "click a session in
  the sidebar", and Desktop keeps session history separate from the CLI.

Criteria 3 and 4 are therefore amended to:

> Codex/Claude account rotation restarts and resumes **the exact task in the
> same workspace** automatically, via committed state plus handoff, with no user
> re-explanation.

Everything else in the direction document stands, including the 95% cap, the
3-point preparation offset, and the core rule that Omni Route only manages
lifecycle and accounts.

---

## Empirical gates before production code

Ordered. Each can invalidate a decision above.

1. Install Codex Desktop and Claude Desktop.
2. `CLAUDE_CONFIG_DIR` isolation: two profiles produce two distinct accounts.
   On failure, fall back to per-account `--user-data-dir`.
3. `CODEX_HOME` isolation: swapping `auth.json` changes the Desktop account.
4. **Stop-hook injection**: a hook can get the running agent to write the
   handoff before the turn ends. This is the last load-bearing unknown. If it is
   not supported, the supervisor must synthesise the handoff from git state
   instead — a real quality drop that goes back to the user as a decision.
5. A self-gating automation fires and actually starts a turn after a relaunch,
   on both apps.
6. Full unattended loop: Codex A -> Codex B, Claude A -> Claude B.
