# Omni Route / Omnigent Subscription Rotation v1.1

An isolated patched Omnigent installation for macOS that rotates across your
registered Codex subscription accounts. Claude Pro is an optional final fallback.

It uses official CLI subscription authentication. It does not turn ChatGPT Plus
or Claude Pro into API keys.

## Full macOS install

From a fresh clone, this installs the normal Omnigent CLI, the current Omnigent
Desktop app, and Omni Route:

```bash
git clone git@github.com:Sevastian-Bahynskyi/omni-route.git
cd omni-route
./install_all.sh
```

The script uses Homebrew for missing shared prerequisites, Omnigent's official
CLI installer, and Omnigent's official macOS DMG download. It then runs the
Omni Route installer and subscription configurator.

Shared tools such as Homebrew, Git, `uv`, `tmux`, Node and Codex CLI are treated
as machine-level dependencies rather than Omnigent-owned files.

## Omni Route only

If the normal Omnigent CLI/Desktop app are already installed and you only want
the routing extension:

```bash
./install.sh
```

The installer opens a small subscription setup loop:

```text
Commands: codex, claude, done

Add [codex/claude] or type done:
```

Type `codex` each time you want to register another ChatGPT/Codex subscription.
There is no fixed two-account limit. Type `claude` if you want to enable your
currently authenticated Claude Pro account as the final fallback. Claude is
optional, so you can install now with only your Codex accounts.

When the route looks right, type `done` (or `confirm` / `finish`).

Example today:

```text
codex
codex
done
```

Later, after getting Claude Pro:

```bash
~/.local/bin/omni-rotate-accounts
```

Then:

```text
claude
done
```

## Run

```bash
~/.local/bin/omni-rotate codex
```

The normal CLI remains available separately as:

```bash
omni
```

## Full read-only diagnostics

Run the diagnostic script directly with:

```bash
~/.local/bin/omni-rotate-test
```

It reports explicit `[PASS]`, `[WARN]`, `[FAIL]`, and `[INFO]` lines and finishes
with `READY`, `READY WITH WARNINGS`, or `NOT READY`.

The diagnostic checks:

- required commands and patched launchers;
- patched Omnigent executable and all rotation integration points;
- synthetic A -> B rotation using the real pool implementation without touching
  your real state;
- current and legacy quota payload parsing plus `usageLimitExceeded` detection;
- every configured Codex account auth file and file permissions;
- every account with the official `codex login status` command under its own
  isolated `CODEX_HOME`;
- duplicate account identities;
- current selected account and cooldown state;
- Claude fallback when configured;
- when an Omni Route Codex session is running, the live bridge account binding
  and read-only Codex `account/read` + `account/rateLimits/read` RPCs.

No model turn is sent by the diagnostic, and it does not modify route config,
cooldowns, or subscription credentials. If no live Codex session exists, the
live app-server check is a warning rather than a failure.

## Local status dashboard

Start the dashboard with:

```bash
~/.local/bin/omni-rotate-status
```

It opens `http://127.0.0.1:8787/` and refreshes every two seconds. The Matrix-style
page shows:

- configured Codex accounts and their order;
- account email when it can be read from the local auth metadata/JWT payload;
- current/ready/cooldown/missing-auth state;
- cooldown reset countdowns when known;
- current active account and rotation threshold;
- Claude fallback configuration/auth status;
- patched runtime, normal `omni` CLI, and Desktop app presence.

The page includes **RUN FULL TEST**, which runs the same read-only diagnostic and
renders its PASS/WARN/FAIL output in a Matrix-style terminal panel.

The server binds only to `127.0.0.1`. Status and diagnostics use GET/HEAD only;
POST/PUT/PATCH/DELETE return HTTP 405. The status API may expose your own account
email, but never returns OAuth/JWT token contents or auth file paths.

Options:

```bash
~/.local/bin/omni-rotate-status --no-open
~/.local/bin/omni-rotate-status --port 8899
```

## Routing behavior

The Codex accounts are tried in the order they were registered. Before a fresh
turn, the extension calls Codex app-server's `account/rateLimits/read`. At 99%
usage, when ordinary included usage is denied, or when Codex reports a reached
limit, Omnigent relaunches the same session on the next available Codex account.

If a running turn itself fails with Codex's structured `usageLimitExceeded`, the
extension rotates and issues a continuation on the same resumed Codex thread.

If every Codex subscription is exhausted:

- with Claude configured: the same Omnigent session switches to
  `claude-native-ui` and continues;
- without Claude configured: the session reports that no subscription fallback
  remains instead of failing installation or trying an unconfigured Claude CLI.

Only `auth.json` changes between Codex accounts. Omnigent's normal private
`CODEX_HOME` construction remains responsible for config, skills, MCP setup,
hooks and workspace/session state.

## Current Claude limitation

The route currently supports one Claude Pro fallback, because Omnigent's native
Claude harness uses the active Claude Code CLI login. Codex subscription slots
are unlimited. Multi-Claude-account pooling would be a separate runtime feature.

## Full macOS cleanup

```bash
./uninstall_all.sh
```

It requires typing `DELETE`. For non-interactive use:

```bash
./uninstall_all.sh --yes
```

This removes the normal Omnigent CLI, Desktop app and Desktop data,
`~/.omnigent`, Omni Route runtime/account profiles, diagnostic/status launchers,
Omnigent backups, `~/omnigent`, and this cloned `omni-route` repository after
verifying its Git origin.

It intentionally keeps shared developer tools and unrelated coding CLIs:
Homebrew, Git, `uv`, `tmux`, Node, Codex CLI and Claude CLI.

## Uninstall Omni Route only

```bash
./uninstall.sh
```

This leaves the normal Omnigent installation and registered subscription login
profiles untouched.
