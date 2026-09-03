# Omni Route / Omnigent Subscription Rotation

Omni Route is a macOS extension around Omnigent that rotates one routed coding
session across any number of ChatGPT/Codex and Claude subscription accounts in one
ordered pool. It uses the official Codex/Claude CLI subscription
logins; it does not convert subscriptions into API keys.

> **Coding agents:** read [`AGENT_HANDOFF.md`](AGENT_HANDOFF.md) completely before modifying this repository. It contains the canonical product context, architecture decisions, invariants, testing expectations, and repository workflow.

## Full install

```bash
git clone git@github.com:Sevastian-Bahynskyi/omni-route.git
cd omni-route
./install_all.sh
```

`install_all.sh` installs/updates the normal Omnigent CLI, Omnigent Desktop app,
required shared dependencies when missing, and Omni Route. `install.sh` can be
used when normal Omnigent/Desktop are already present.

Tailscale is installed automatically when missing so secure remote dashboard
access is available without a separate install step. Tailscale account sign-in
and connection are still explicit user actions.

During account setup, enter `codex` or `claude` once per subscription, then
`done`. Either provider can be first, and Claude-only pools are supported.
Each login gets a separate named profile; rerunning setup preserves existing
accounts, route settings, cooldowns and session bindings. Existing Claude
fallback configuration migrates to a regular `claude-legacy` profile.

Codex uses isolated `CODEX_HOME` credentials. Claude uses isolated
`CLAUDE_CONFIG_DIR` profiles, including distinct macOS Keychain entries as
documented in [Claude Code authentication](https://code.claude.com/docs/en/authentication).
Use a current Claude Code release. Normal CLI logins remain unchanged.

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
provider, installation health and diagnostics. Accounts from either provider can be dragged in
the route chain; the saved order is the actual priority used for new account
selection and quota failover.

The **Current account** dropdown switches the latest routed Omnigent session
without changing route priority:

- Codex account -> another Codex account: waits for the current turn to become
  idle, binds the requested subscription and relaunches the same session.
- Codex -> selected Claude account: uses Omnigent's native agent switch after the turn is idle.
- Claude -> Codex: binds the selected Codex subscription and switches the same
  Omnigent session back to `codex-native-ui`.
- Claude account -> another Claude account: relaunches with the selected isolated login.

A stopped/resumable Codex session can also have its next Codex account selected;
the binding is used when that session resumes.

### Optional Tailscale remote access

The **Remote Access** section shows Tailscale connection state and can enable or
disable a tailnet-only HTTPS proxy to the existing local dashboard. Enabling it
runs Tailscale Serve on dedicated HTTPS port `8443` and shows the generated
`https://<device>.<tailnet>.ts.net:8443/` URL with a **Copy Link** action.

The dashboard server itself continues listening only on `127.0.0.1:8787`.
Tailscale terminates HTTPS and forwards authenticated tailnet traffic to that
loopback service. Omni Route does not use Tailscale Funnel and does not expose
the dashboard to the public internet. If port `8443` is already used by another
Tailscale Serve target, Omni Route reports the conflict instead of overwriting
that configuration.

## Session history import

Every install automatically scans local Codex and Claude histories and imports
them into Omnigent. You can rerun it at any time:

```bash
omni-rotate sessions
```

The importer:

- scans normal `~/.codex`, every registered Omni Route Codex account home, the
  active Claude config home, and registered Omni Route Claude account homes;
- uses Omnigent's own Codex/Claude transcript normalizers, preserving native
  titles, workspaces and resumable external session IDs;
- creates/reuses first-class Omnigent projects by transcript workspace/cwd, so
  sessions from the same repository are grouped together;
- excludes Codex subagent sessions by session metadata and Claude subagent transcripts;
- deduplicates shared Codex thread IDs across different account homes;
- when shared copies differ, normalizes all copies and keeps the richest
  transcript (most items, then newest/largest copy);
- treats already-imported sessions as existing instead of creating duplicates.

## Routing behavior

Accounts are attempted in dashboard route order regardless of provider. Before a
new Codex turn Omni Route checks Codex rate limits. At the configured threshold
(default 99%), or on a provider usage-limit error, the current account is cooled
down and the same Omnigent session continues on the next available account.
Claude accounts participate in the same routing and cooldown logic. Provider
changes preserve the workspace and accumulated conversation context. When all
accounts are unavailable the router reports exhaustion until an account recovers.

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
`tmux`, Node, Codex CLI, Claude CLI and Tailscale.
