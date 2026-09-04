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
Sessions and rollouts never move.

Note: "Codex Desktop" is **`/Applications/ChatGPT.app`**. The standalone
`Codex.app` is legacy — its Homebrew cask is deprecated upstream with
`chatgpt` named as the replacement — and the `codex` CLI itself resolves the
desktop app to `ChatGPT.app`, reporting it under "Desktop App" in
`codex doctor`.

**Claude** — one Electron `--user-data-dir` per account. This is the
pre-authorised fallback, adopted because gate 2 disproved the first choice:
Claude Desktop's account identity lives in its Electron user-data directory,
not in `CLAUDE_CONFIG_DIR`. See "Gate results" below.

`CLAUDE_CONFIG_DIR` is still set alongside it, pointing at the matching
`~/.omnigent/claude-accounts/claude-N` profile, because Claude Code
configuration — settings, hooks, scheduled tasks — is read from there. The two
are set together: the user-data-dir carries the *account*, the config dir
carries the *configuration*.

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

---

## Gate results

Run 2026-09-04 on macOS 26.6.2 (arm64).

### Gate 1 — install both desktop apps: **PASS**

- **Codex Desktop = `/Applications/ChatGPT.app`**, version 26.831.21537, already
  installed. `codex doctor` reports "the desktop application is installed" and
  resolves the `codex` binary from inside the bundle
  (`/Applications/ChatGPT.app/Contents/Resources/codex`). The separate
  `Codex.app` cask is deprecated upstream ("replacement: `brew install --cask
  chatgpt`"), so no second app is installed.
- **Claude Desktop** installed via the official Homebrew cask (sourced from
  `claude.com/download`): `com.anthropic.claudefordesktop` 1.46388.2, Electron.

### Gate 2 — Claude account isolation: **FAIL for `CLAUDE_CONFIG_DIR`, PASS for `--user-data-dir`**

Launching `Claude.app` with `CLAUDE_CONFIG_DIR` set to an empty directory left
that directory untouched, and did not write to `~/.claude` either. The app wrote
its state to `~/Library/Application Support/Claude/` — a standard Electron
user-data directory (`Cookies`, `Local Storage`, `IndexedDB`, `Preferences`).
The account is therefore a web session held in the user-data directory, and
`CLAUDE_CONFIG_DIR` cannot isolate it.

Launching with `--user-data-dir=<path>` created a complete, separate profile
(26 entries including its own `Cookies` and `Local Storage`) within 7 seconds.

**Consequence:** decision §2 is amended. Claude account isolation uses
`--user-data-dir`, with `CLAUDE_CONFIG_DIR` set alongside it for configuration.
Each account needs a one-time interactive sign-in in its own profile, because a
fresh user-data directory starts logged out. This is a user action and must be
surfaced by the dashboard as `needs user action`, not attempted automatically.

### Gate 3 — Codex account isolation: **PASS**

`codex app-server` speaks newline-delimited JSON-RPC over stdio and honours
`CODEX_HOME`. `initialize` echoes back the resolved `codexHome`, and
`account/read` returns the authenticated account:

| CODEX_HOME | account |
| --- | --- |
| `~/.omnigent/codex-accounts/codex-1` | `support@…` (plus) |
| `~/.omnigent/codex-accounts/codex-2` | `baginski.play@…` (plus) |
| `~/.codex` (default) | `support@…` (plus) |

`account/rateLimits/read` confirms the window shapes assumed in §3:
`primary.windowDurationMins = 300` (the 5-hour window) and
`secondary.windowDurationMins = 10080` (weekly).

Two further consequences:

- The pre-flight identity check of §7 needs no network call and no app-server at
  all in the cheap path: `auth.json` embeds an `id_token` whose claims carry the
  account email and `chatgpt_account_id`.
- The Omnigent-coupled quota read is replaced. `codex_app_client.py` implements
  the client directly, with no Omnigent import.

### Gate 4 — Stop-hook injection: **PASS**

The load-bearing unknown is resolved. A `Stop` hook **can** make the running
agent do additional work before the turn ends.

Mechanism: the hook exits with **code 2** and writes the instruction to
**stderr**. The instruction is fed back to the agent, which acts on it and then
stops. JSON output is not needed and `decision`/`reason` fields are not
required.

Verified end to end in a scratch workspace: the hook told the agent it was about
to lose quota and must write `handoff.md`. The agent wrote the file with the
exact required contents and reported "Done — handoff.md written with the
required contents." Confirmed working in headless `claude -p` mode, so it does
not depend on an interactive session.

**Mandatory implementation detail:** the hook must be guarded by a marker file.
Exit 2 prevents stopping, so an unguarded Stop hook loops forever. The test hook
wrote `.hook-fired` on first invocation and exited 0 on every later one. The
production hook must do the same, keyed per rotation, and must exit 0 whenever
no rotation is armed.

This confirms §4 (preparation-threshold wrap-up and turn-boundary rotation) and
§5 (outgoing agent writes the handoff) are implementable as specified.

### Gate 5 — automation registration: **PASS on mechanism, write-path not yet verified**

A scheduled task created through the Claude Desktop UI stores its two halves in
two places:

1. **The prompt**, on disk and plainly writable, at
   `~/.claude/scheduled-tasks/<id>/SKILL.md` — YAML frontmatter carrying `name`
   and `description`, with the prompt as the body.
2. **The metadata**, as plain JSON (not leveldb) at
   `<user-data-dir>/claude-code-sessions/<accountUuid>/<uuid>/scheduled-tasks.json`.

The observed record for a Manual task:

```json
{"id":"omni-route-probe","displayName":"omni-route-probe","enabled":true,
 "filePath":"/Users/seva/.claude/scheduled-tasks/omni-route-probe/SKILL.md",
 "createdAt":1788533213585,"cwd":"/Users/seva/Developer/personal/omni-route",
 "useWorktree":false}
```

The full field set, recovered from the application bundle:

```
id, displayName, cronExpression, fireAt, enabled, filePath, createdAt, model,
userSelectedFolders, userSelectedFiles, userSelectedProjectUuids, spaceId, cwd,
useWorktree, sourceBranch, permissionMode, chromePermissionMode, disableJitter,
notifySessionId, dispatchSubscribed, migratedFromRemote
```

Scheduling semantics, from the same source: a task is recurring when it has a
`cronExpression`, and one-shot when it has a `fireAt`. Setting either enables the
task; clearing both disables it. "Manual" is simply a task with neither, which
is why the probe task has `enabled: true` and no schedule field and will never
fire on its own.

**Consequence — the automation is programmatically registrable.** Omni Route can
create the self-gating rotation automation by writing `SKILL.md` and appending
one record to `scheduled-tasks.json`. No per-account UI setup is needed beyond
the one-time sign-in, and because `cwd` is per-task, Omni Route can create one
automation per workspace on demand rather than asking the user to do it for every
project.

Settings the rotation automation must use:

- `cronExpression: "*/5 * * * *"` — the ~5 minute self-gating tick.
- `useWorktree: false` — **required**. A worktree would put the continuation in a
  new tree, away from the branch the outgoing agent committed to.
- `disableJitter: true` — the app otherwise staggers runs by a few minutes, which
  is latency added to every rotation.
- `permissionMode` set for unattended work; the UI's default is "Don't ask".

**Still unverified:** that a task written this way is picked up by the app. The
schema is known and the file is plain JSON, but writing it has not yet been
tested. It must only be written while the app is stopped, which the rotation
sequence already guarantees.

**Risk to carry:** `scheduled-tasks.json` is an undocumented internal format.
The writer must validate the file's shape before touching it, back it up, and
degrade to a visible `needs user action` ("create the automation manually")
rather than corrupting the file if the schema stops matching.

### Gate 6 — not yet run

Full unattended loop: Codex A -> Codex B, Claude A -> Claude B.

### Sign-in findings (from a remote-controlled run)

- Claude Desktop demanded a **web sign-in even though `claude-1` and `claude-3`
  are already authenticated as CLI profiles**. Independent confirmation of the
  gate 2 conclusion: the Desktop account is a browser session, unrelated to
  `CLAUDE_CONFIG_DIR`.
- The app partitions its per-account state by **account UUID**, so the
  `claude-code-sessions/<accountUuid>/` path gives Omni Route a reliable way to
  find the right task store per account.
- Profile-to-account mapping: `claude-1` = `couplegoai.main@…`,
  `claude-3` = `support@couplegoai.com`, `claude-2` = not signed in and not in
  the pool. ChatGPT.app is signed in as `support@couplegoai.com` (Plus).
- ChatGPT.app **blocks UI self-automation**, so any plan that depends on driving
  its interface is unsafe. This reinforces the decision not to use UI automation
  for rotation.
