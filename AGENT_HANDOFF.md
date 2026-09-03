# Omni Route — Canonical Agent Handoff

> Purpose: give a new coding agent enough context to continue Omni Route correctly without access to the conversation that created it.
>
> This document is written for agents, not end users. Read it completely before proposing architecture changes or editing the repository.

## 1. Source of truth and working rules

Repository:

`Sevastian-Bahynskyi/omni-route`

The `main` branch is the canonical source of truth. Do not treat old ZIPs, copied snippets, chat messages, or local artifacts as authoritative when they conflict with current `main`.

Important user instruction for this repository:

**Every completed code/file change must be committed and pushed to `main` before reporting it as finished.**

Use the simplest GitHub workflow appropriate to the change. For ordinary edits, prefer direct file reads/writes. Use lower-level Git blob/tree plumbing only when there is a concrete need for one atomic multi-file commit.

Before modifying behavior:

1. Read the current relevant files from `main`.
2. Preserve existing user-facing commands unless the user explicitly asks to break compatibility.
3. Test the changed path as far as the available environment allows.
4. Commit and push.
5. Report what changed, what was actually tested, and any environment limitation. Do not claim live behavior was proven if only syntax/synthetic tests were possible.

Communication style expected by the user: direct, technical, concise, implementation-oriented. Avoid long generic explanations when an exact command or concrete result is available.

---

## 2. Why Omni Route exists

The user wants to use subscription-based coding agents for long-running development without paying API-token pricing and without manually changing accounts when a subscription quota is exhausted.

The core desired route is:

```text
One Omnigent session
        |
        v
Codex subscription account 1
        |
        | quota near/exhausted
        v
Codex subscription account 2
        |
        | quota near/exhausted
        v
Codex subscription account 3 ...
        |
        | all Codex accounts unavailable
        v
optional Claude Pro / Claude Code fallback
```

The number of Codex subscriptions is intentionally arbitrary. The implementation must not assume only two accounts.

The important UX requirement is not merely “launch another CLI.” The user wants **one logical Omnigent session** to survive provider/account changes:

- same Omnigent conversation/session;
- same workspace/project;
- same Codex thread/rollout when rotating between Codex accounts;
- same accumulated Omnigent transcript/context;
- same Desktop/browser/mobile view of the session;
- automatic continuation after quota-triggered failover;
- no need for the user to re-explain the task after each account switch.

Claude is currently an optional final fallback. The architecture currently supports one configured Claude fallback, not an arbitrary Claude-account pool.

No OpenAI or Anthropic API key should be introduced as a substitute for the subscription flow. The design intentionally uses the official Codex CLI / Claude Code subscription login mechanisms.

---

## 3. High-level architecture

Omni Route is an extension around a pinned Omnigent checkout rather than a replacement for Omnigent.

Pinned upstream Omnigent commit:

`2b13f2d7d85431c06e510d3c707c0c6d9a191a44`

At install time `install.sh`:

1. verifies macOS and required tools;
2. clones that exact Omnigent commit into an isolated directory;
3. applies the Omni Route patch with `apply_patch.py`;
4. installs Omnigent dependencies into an isolated `uv` environment;
5. runs extension self-tests/compilation checks;
6. configures subscription profiles;
7. installs the Omni Route dispatcher/diagnostics/dashboard/import helpers;
8. adds `omni-rotate` to PATH;
9. imports existing Codex/Claude session history into Omnigent projects;
10. launches the local dashboard.

Installed patched runtime root:

`~/.local/share/omnigent-subscription-rotation/`

Primary command:

`~/.local/bin/omni-rotate`

The normal upstream Omnigent installation is conceptually separate. Omni Route should not silently replace or mutate the user's normal `omni` command.

---

## 4. User-facing commands

The intended interface is a single `omni-rotate` front door:

```bash
omni-rotate start       # start a routed session
omni-rotate test        # run diagnostics
omni-rotate status      # open the local control dashboard
omni-rotate accounts    # add/configure subscription accounts
omni-rotate sessions    # import/re-sync Codex + Claude history
omni-rotate help
```

Compatibility aliases intentionally remain:

```bash
omni-rotate codex       # alias of `omni-rotate start`
omni-rotate-test
omni-rotate-status
omni-rotate-accounts
```

The word `start` is deliberate. The routed session is not “Codex only”; it starts in the Codex pool and may later fail over to Claude.

The dispatcher is `omni_rotate.sh`. Unknown subcommands are forwarded to the patched Omnigent CLI.

Typical workflow:

```bash
cd ~/some/project
omni-rotate start
```

After that, the user may work from Omnigent's terminal/web/Desktop UI rather than continuing to type in the launching shell.

Do not tell the user to run a second normal `omni codex` instance on the same local Omnigent port while the routed instance is running.

---

## 5. Subscription-account model

Persistent configuration:

`~/.omnigent/codex-account-pool.json`

Persistent runtime state:

`~/.omnigent/codex-account-pool-state.json`

Codex profile homes:

`~/.omnigent/codex-accounts/<profile>/`

Each Codex account has its own `auth.json`. Authentication is done using the official Codex CLI with a dedicated `CODEX_HOME` and file-backed credential storage.

Conceptually:

```bash
CODEX_HOME=<account-home> codex -c 'cli_auth_credentials_store="file"' login
```

Important design constraint:

**Do not overwrite the user's normal `~/.codex/auth.json`.**

The user's normal `~/.codex` remains useful as the source for ordinary Codex configuration, skills, MCP configuration, etc. Omni Route injects only the selected account auth into Omnigent's private per-session Codex home.

File-backed credential storage is intentional on macOS. Without it, Keychain behavior can collapse apparently separate profiles back onto the same login.

Account setup is handled by `configure_subscriptions.py` / `setup_accounts.sh`.

Interactive commands are conceptually:

```text
codex   -> add another independent Codex subscription
claude  -> configure the optional Claude fallback
done    -> finish
```

Existing configured profiles should normally be preserved when setup is rerun.

---

## 6. Automatic Codex rotation

The account-pool implementation lives primarily in:

- `payload/codex_account_pool.py`
- `payload/codex_account_rotation.py`

The account array order in `codex-account-pool.json` is the automatic priority order.

New/unbound sessions select the first available configured account in route order. Existing bound sessions stay on their account until a rotation or manual provider switch requires a change.

Default rotation threshold:

`99%`

There are two main quota-detection paths:

1. **Preflight** before a fresh Codex turn using Codex app-server `account/rateLimits/read`.
2. **Mid-turn structured quota error detection** for a usage-limit failure already returned by Codex.

The implementation handles current and older field naming variants where relevant (for example camelCase and legacy snake_case quota/error shapes).

When an account is exhausted:

1. it is placed on cooldown until a known reset time, or a conservative fallback cooldown if no reset is available;
2. the session is rebound to the next available Codex account in route order;
3. the same Omnigent session is relaunched;
4. the same Codex thread/rollout is resumed;
5. if the previous turn was interrupted by quota, Omni Route injects a continuation instruction so work continues automatically.

If no Codex account is available and no Claude fallback exists, the runtime enters an exhausted state with a clear detail rather than silently failing over to an API provider.

---

## 7. Claude fallback

Claude fallback is optional.

Expected agent name in the current Omnigent integration:

`claude-native-ui`

The intended authentication path is the official Claude Code subscription login, not `ANTHROPIC_API_KEY`.

When all Codex accounts are unavailable and Claude fallback is configured:

1. Omni Route publishes a pending fallback runtime generation so the Codex-side executor can finish cleanly;
2. waits for the Omnigent session to become idle;
3. uses Omnigent's native `switch-agent` API to switch the **same session** to Claude;
4. preserves transcript and workspace;
5. for automatic quota fallback, sends a continuation instruction so Claude continues the interrupted task.

Current limitation: one Claude fallback is supported. Multi-Claude subscription pooling has not been implemented.

---

## 8. Manual provider/account switching

The dashboard now has a **Current provider** selector.

This is separate from route priority.

Route drag/drop answers:

> “What order should automatic failover use?”

Current-provider selection answers:

> “What should this routed session use now?”

Supported intended transitions:

- Codex account A -> Codex account B
- Codex -> Claude
- Claude -> selected Codex account

Manual switching should:

- wait for the active turn to become idle rather than cutting through a live response;
- keep the same Omnigent session/workspace;
- keep route ordering unchanged;
- not falsely mark the previous account quota-exhausted;
- not inject the automatic quota-fallback continuation prompt.

Relevant helpers:

- `switch_provider.py`
- `status_server_ext.py`
- `dashboard.html`

When adding provider-switch functionality, do not turn a manual choice into permanent route-priority mutation unless the user explicitly requests that behavior.

---

## 9. Dashboard

Default local dashboard:

`http://127.0.0.1:8787/`

The dashboard is intentionally a local Matrix/terminal-style control panel.

It is **not read-only anymore**.

Current purposes include:

- router enabled/configured state;
- currently selected/routed account/provider;
- configured account identities/emails where locally discoverable;
- auth state;
- route order;
- cooldown/reset information;
- Claude fallback state;
- installation status;
- diagnostics;
- drag/drop route reordering;
- current-provider switching.

Dashboard status refreshes periodically.

Route ordering is persisted back to `codex-account-pool.json` and is authoritative for future automatic selection/failover.

A route reorder should not forcibly interrupt a currently bound session. A live rotation reads fresh pool configuration so the changed order affects the next failover without requiring a full restart.

`install.sh` starts/restarts the dashboard and opens it after successful installation.

`omni-rotate status` should reuse/reopen a dashboard already serving on the default port rather than failing because a second server cannot bind to 8787.

The dashboard is local-only by default. Do not expose it publicly without an explicit security design.

---

## 10. Session-history import

Importer:

`import_sessions.py`

Command:

```bash
omni-rotate sessions
```

Installation runs the importer automatically, but an import failure is non-fatal to the core installation and can be retried later.

The purpose is to make pre-existing Codex/Claude sessions visible inside Omnigent rather than starting with an empty Omnigent history.

The importer uses Omnigent's own native session-normalization/import mechanisms where possible. It should preserve:

- native/external session IDs;
- workspace/cwd;
- native or derived titles;
- normalized conversation items;
- resumability/provenance information.

Sessions are grouped into real Omnigent projects based on their workspace/repository path so the resulting organization resembles the project grouping users see in Codex/Claude tools.

### Shared Codex sessions

This is an explicit requirement.

The same Codex thread can appear under more than one account/profile because a session may have been resumed or used across account changes.

Do **not** import such a thread once per account.

Deduplicate by Codex external/thread session identity across all scanned Codex homes.

If copies of the same thread differ, normalize the candidate copies and retain the richest representation (primarily most normalized items, with recency/size as tie-break information). The goal is to avoid losing history when a shared session was continued under another account.

Rerunning the importer should be idempotent: already imported sessions should be recognized/skipped/reused rather than duplicated.

---

## 11. Omnigent project/session semantics to preserve

Omnigent projects are first-class containers for sessions. Session membership should use Omnigent's project/session model rather than a parallel Omni Route database.

When working on import/grouping logic:

- create/reuse projects from workspace identity;
- preserve the workspace in project configuration where appropriate;
- import sessions through Omnigent's import/session APIs rather than writing arbitrary rows directly unless there is a compelling verified reason;
- keep native external session IDs so resume behavior remains possible.

The user wants imported history to feel native in Omnigent Desktop/web, not like a separate Omni Route history page.

---

## 12. Desktop, browser and future remote use

Omnigent Desktop/web are intended to be views of the same local Omnigent server/session started by Omni Route.

Conceptually:

```text
Mac
  Omnigent server + Omni Route router
      |
      +-- terminal client
      +-- browser UI
      +-- Omnigent Desktop
      +-- future secure remote/mobile client
```

The user wants to eventually leave the Mac running and control the same sessions remotely from a phone while the Mac continues rotating accounts automatically until available subscriptions are exhausted.

This remote-access feature is **not the core account-rotation implementation and should not move execution to the phone**. The intended model is:

- Mac remains execution host;
- Codex/Claude CLIs and subscription credentials remain on the Mac;
- Omni Route continues account/provider switching locally;
- a phone/mobile client connects securely to the Omnigent server;
- remote exposure must use a secure authenticated HTTPS/tunnel design, not raw public exposure of localhost/dashboard endpoints.

Remote integration is a future/adjacent feature unless current `main` contains newer implementation.

---

## 13. Installation layers

### `install.sh`

Installs/rebuilds Omni Route around an existing macOS developer setup. It clones the pinned Omnigent checkout, applies the patch, runs tests, configures accounts, installs commands, imports histories and opens the dashboard.

It requires Codex CLI to already exist.

It should preserve a previous patched checkout long enough to roll back if installation fails.

### `install_all.sh`

This is the user-facing “fresh/full setup” script.

Its responsibility is broader: ensure normal Omnigent CLI/Desktop plus shared prerequisites and then run Omni Route installation.

The user expects to be able to use this when rebuilding from scratch on a Mac.

Do not unnecessarily remove shared developer dependencies during install/uninstall.

---

## 14. PATH behavior

The user specifically requested that `omni-rotate` work from any directory immediately after install.

`path_helpers.sh` manages this.

The installation currently uses both:

- a managed shell-profile PATH block for `~/.local/bin`;
- a conservative Homebrew-bin shim/symlink so the command is usable immediately in the current environment.

Uninstall must remove only Omni Route's managed PATH/shim entries, not unrelated user shell configuration.

Do not overwrite an unrelated existing `omni-rotate` executable/symlink without an explicit migration decision.

---

## 15. Diagnostics

Primary command:

```bash
omni-rotate test
```

The diagnostics are designed to give explicit:

```text
[PASS]
[WARN]
[FAIL]
[INFO]
```

with a final readiness summary.

Important checks include:

- macOS platform;
- required commands (`codex`, `uv`, `tmux`);
- optional normal `omni` presence;
- installed Omni Route launchers/runtime;
- patched Omnigent executable/version;
- expected runtime patch markers/wiring;
- synthetic account A -> B rotation using temporary state;
- pool configuration and threshold;
- each configured Codex auth file and permissions;
- official Codex `login status` in each isolated account home;
- distinct account identities (duplicate account identity is a real failure);
- pool state/cooldowns;
- optional Claude state;
- live Codex bridge/app-server account and quota checks when a routed session is running.

Warnings that can be legitimate:

- normal `omni` not found while `omni-rotate` itself is healthy;
- Claude fallback not configured when the user intentionally has no Claude subscription;
- no live Codex bridge before `omni-rotate start` has been launched.

A duplicate Codex identity is not benign: two profile names that authenticate to the same ChatGPT account do not provide additional quota.

The diagnostic should never print access tokens/JWT contents.

---

## 16. Cleanup behavior

### `uninstall.sh`

Removes Omni Route/patched runtime and managed command/PATH/dashboard pieces while leaving normal Omnigent and the user's subscription profile data unless current code explicitly documents otherwise.

### `uninstall_all.sh`

Full cleanup is destructive and intentionally requires confirmation unless `--yes` is passed.

It removes normal Omnigent, Desktop app/data, Omni Route runtime/state and the verified cloned repo as implemented by current code.

Shared developer tools should remain, including things such as:

- Homebrew
- Git
- `uv`
- `tmux`
- Node
- Codex CLI
- Claude CLI

Do not casually broaden cleanup to unrelated user files.

---

## 17. Important implementation files

Read current versions from `main` before editing.

```text
README.md
    User-oriented overview and commands.

AGENT_HANDOFF.md
    This canonical context/handoff document.

MANIFEST.txt
    Compact implementation manifest.

install.sh
    Omni Route install/rebuild.

install_all.sh
    Full macOS setup including normal Omnigent/Desktop.

uninstall.sh
    Omni Route-only cleanup.

uninstall_all.sh
    Full destructive cleanup.

path_helpers.sh
    PATH and command-shim management.

omni_rotate.sh
    Main `omni-rotate` dispatcher.

configure_subscriptions.py
    Interactive Codex/Claude subscription setup.

setup_accounts.sh
    Entry point for account configuration.

apply_patch.py
    Applies Omni Route changes to the exact pinned Omnigent checkout.

self_test.py
    Patch/account-pool synthetic self-tests.

diagnose.py
    User-facing full diagnostic suite.

dashboard.html
    Matrix-style route/status/provider UI.

status_server.py
    Base local dashboard/status service.

status_server_ext.py
    Writable provider-switch/status extension layer.

switch_provider.py
    Manual current-provider/account switching helper.

import_sessions.py
    Codex/Claude history discovery, dedupe, project grouping and import.

payload/codex_account_pool.py
    Persistent pool, selection, cooldown, auth binding and quota interpretation.

payload/codex_account_rotation.py
    Live rotation monitor, relaunch and Claude fallback behavior.
```

---

## 18. Why some alternative approaches were rejected

### Wrapping Codex TUI with an external “quota auto-switch” script

Rejected because Omnigent native Codex integration depends on the Codex `app-server` JSON-RPC/socket contract. A wrapper that kills Codex and starts `codex resume` outside Omnigent breaks that integration and can lose the correct bridge/session lifecycle.

Account rotation therefore lives inside the Omnigent/Codex integration path.

### Replacing subscriptions with API keys

Rejected because the user's requirement is subscription usage and subscription quotas. Per-token API usage is specifically not the desired cost model.

### Overwriting normal `~/.codex` auth

Rejected because it destroys account isolation and risks changing the user's ordinary Codex login.

### Treating Dashboard route order as cosmetic

Rejected. Saved dashboard order must actually drive automatic account selection/failover.

### Importing the same Codex thread once per account

Rejected because sessions can be shared/resumed across accounts. External session identity is the dedupe key, not the account profile name.

---

## 19. Safety and invariants for future changes

Preserve these unless the user explicitly changes the product requirements:

1. **One logical Omnigent session survives account/provider changes.**
2. **No API-key substitution for subscription routing.**
3. **Normal `~/.codex` authentication is not overwritten.**
4. **Arbitrary number of Codex accounts.**
5. **Route order is user-controlled and authoritative.**
6. **Manual provider switching is distinct from automatic failover order.**
7. **Shared Codex session IDs are deduplicated across account homes.**
8. **Normal Omnigent can coexist with Omni Route.**
9. **Do not run competing normal/patched local servers on the same port.**
10. **Local dashboard write APIs should remain loopback/same-origin protected unless a real remote-security layer is designed.**
11. **Do not expose credentials/tokens in diagnostics or dashboard responses.**
12. **Every completed repository change is committed and pushed to `main`.**

---

## 20. Testing expectations for changes

At minimum, choose tests relevant to the change and state exactly what was run.

Typical checks:

```bash
python3 -m py_compile <changed python files>
bash -n install.sh install_all.sh uninstall.sh uninstall_all.sh
python3 self_test.py
python3 import_sessions.py --self-test
```

For an actual installed Mac, also use:

```bash
omni-rotate test
omni-rotate start
# then, in another terminal:
omni-rotate test
```

For dashboard changes, verify:

- status endpoint loads;
- route reorder persists;
- provider endpoint rejects invalid targets;
- provider switching waits for idle where required;
- dashboard can be reopened while its server is already running;
- same-origin protections remain intact.

For session-import changes, explicitly test:

- Codex session discovery;
- Claude session discovery;
- same project/workspace grouping;
- duplicate/shared Codex thread across two account homes;
- richer shared copy wins;
- rerun is idempotent.

Do not deliberately burn subscription quota merely to test failover unless the user explicitly asks for a real state-mutating end-to-end quota test. Synthetic rotation and read-only live quota/app-server checks are preferred for normal diagnostics.

---

## 21. Known limitations / current scope boundaries

Check current `main` in case these have changed.

As of this handoff's creation:

- Omni Route is packaged for macOS.
- Codex pooling supports arbitrary configured Codex accounts.
- Claude is an optional single fallback, not a multi-Claude pool.
- The dashboard runs locally on loopback by default.
- Secure phone/remote access is a desired next capability, not something to assume is already deployed.
- A normal `omni` command may be absent from PATH while patched `omni-rotate` remains healthy; diagnostics treat this as a warning rather than a router failure.
- Full real-world forced quota exhaustion is not part of the ordinary diagnostic path.

---

## 22. If starting a brand-new agent session

A new agent should do this before giving architectural advice:

1. Read this file completely.
2. Read `README.md` and `MANIFEST.txt`.
3. Inspect the exact current files relevant to the requested change.
4. Check current `main` rather than relying on commit hashes quoted in an old chat.
5. Preserve the invariants above.
6. Implement the requested change without asking the user to re-explain decisions already captured here.
7. Test.
8. Commit and push to `main`.
9. Report the pushed commit and concrete behavior.

If a new request appears inconsistent with this document, the user's new explicit request wins. Update this handoff file when a product-level invariant, command, architecture decision, or major capability changes so the next agent inherits the new reality.
