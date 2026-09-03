# Omni Route / Omnigent Subscription Rotation

Omni Route is a macOS extension around Omnigent that rotates one routed coding
session across multiple ChatGPT/Codex subscription accounts and can optionally
fall back to Claude Pro. It uses the official Codex/Claude CLI subscription
logins; it does not convert subscriptions into API keys.

## Full install

```bash
git clone git@github.com:Sevastian-Bahynskyi/omni-route.git
cd omni-route
./install_all.sh
```

`install_all.sh` installs/updates the normal Omnigent CLI, Omnigent Desktop app,
required shared dependencies when missing, and Omni Route. `install.sh` can be
used when normal Omnigent/Desktop are already present.

During account setup, enter `codex` once per Codex subscription, optionally
`claude`, then `done`.

## Commands

Installation adds `omni-rotate` to PATH:

```bash
omni-rotate start       # start the routed session
omni-rotate test        # full diagnostics
omni-rotate status      # open the local control dashboard
omni-rotate accounts    # add/configure subscriptions
omni-rotate sessions    # import/re-sync Codex + Claude local history
omni-rotate help
```

`omni-rotate codex` remains an alias for `omni-rotate start`.

## Control dashboard

The dashboard runs on `http://127.0.0.1:8787/` and opens automatically after a
successful `install.sh`.

It shows account emails/status, the automatic route chain, current routed
provider, installation health and diagnostics. Codex accounts can be dragged in
the route chain; the saved order is the actual priority used for new account
selection and quota failover.

The **Current provider** dropdown switches the latest routed Omnigent session
without changing route priority:

- Codex account -> another Codex account: waits for the current turn to become
  idle, binds the requested subscription and relaunches the same session.
- Codex -> Claude: uses Omnigent's native agent switch after the turn is idle.
- Claude -> Codex: binds the selected Codex subscription and switches the same
  Omnigent session back to `codex-native-ui`.

A stopped/resumable Codex session can also have its next Codex account selected;
the binding is used when that session resumes.

## Session history import

Every install automatically scans local Codex and Claude histories and imports
them into Omnigent. You can rerun it at any time:

```bash
omni-rotate sessions
```

The importer:

- scans normal `~/.codex`, every registered Omni Route Codex account home, the
  active Claude config home, and future Omni Route Claude account homes;
- uses Omnigent's own Codex/Claude transcript normalizers, preserving native
  titles, workspaces and resumable external session IDs;
- creates/reuses first-class Omnigent projects by transcript workspace/cwd, so
  sessions from the same repository are grouped together;
- deduplicates shared Codex thread IDs across different account homes;
- when shared copies differ, normalizes all copies and keeps the richest
  transcript (most items, then newest/largest copy);
- treats already-imported sessions as existing instead of creating duplicates.

## Routing behavior

Codex accounts are attempted in dashboard route order. Before a new turn Omni
Route checks Codex rate limits. At the configured threshold (default 99%), or on
a structured usage-limit error, the current account is cooled down and the same
Omnigent session is relaunched on the next available account. If every Codex
subscription is unavailable and Claude is configured, the same session switches
to Claude and continues automatically.

Manual dashboard provider switches do not mark an account exhausted and do not
inject an automatic continuation prompt.

## Cleanup

Remove only Omni Route:

```bash
./uninstall.sh
```

Full Omnigent + Desktop + Omni Route cleanup:

```bash
./uninstall_all.sh
# or
./uninstall_all.sh --yes
```

The full cleanup keeps unrelated shared tools such as Homebrew, Git, `uv`,
`tmux`, Node, Codex CLI and Claude CLI.
