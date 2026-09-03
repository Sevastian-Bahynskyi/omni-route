# Omni Route / Omnigent Subscription Rotation v1.1

Omni Route is an isolated patched Omnigent installation for macOS that rotates across registered Codex subscription accounts, with optional Claude Pro fallback.

It uses official CLI subscription authentication. It does not convert ChatGPT Plus/Pro or Claude Pro into API keys.

## Full macOS install

From a fresh clone:

```bash
git clone git@github.com:Sevastian-Bahynskyi/omni-route.git
cd omni-route
./install_all.sh
```

This installs/updates the normal Omnigent CLI, Omnigent Desktop app, required shared dependencies when missing, and Omni Route.

## Omni Route only

If normal Omnigent/Desktop are already installed:

```bash
./install.sh
```

The account configurator accepts:

```text
codex
claude
done
```

Type `codex` once per ChatGPT/Codex subscription. Type `claude` to enable the currently authenticated Claude Pro account as final fallback. Claude is optional.

## One command: `omni-rotate`

Installation adds `~/.local/bin` to your macOS shell PATH and installs a managed Homebrew-bin shim so `omni-rotate` is available from any directory immediately after installation.

Primary commands:

```bash
omni-rotate codex                 # start routed Omnigent/Codex
omni-rotate test                  # full read-only diagnostics
omni-rotate status                # Matrix-style localhost dashboard
omni-rotate accounts              # add/configure subscriptions
omni-rotate help
```

Any unrecognized subcommand is forwarded to the patched Omnigent CLI, so other Omnigent commands continue to work through `omni-rotate`.

Compatibility aliases remain available:

```bash
omni-rotate-test
omni-rotate-status
omni-rotate-accounts
```

## Status dashboard

```bash
omni-rotate status
```

Opens `http://127.0.0.1:8787/`. It shows route order, current account, account email when locally available, cooldown/reset state, Claude fallback, installation health, and a Matrix-style diagnostic terminal with a **Run Full Test** button.

The server binds only to `127.0.0.1`. It does not expose OAuth/JWT token contents.

Options:

```bash
omni-rotate status --no-open
omni-rotate status --port 8899
```

## Diagnostics

```bash
omni-rotate test
```

Diagnostics validate the installed runtime, account auth, distinct account identities, permissions, rotation wiring, synthetic A→B switching, current state, Claude fallback, and live Codex account/quota RPCs when a routed session is running.

## Routing behavior

Codex accounts are tried in registration order. Before a fresh turn, Omni Route checks Codex app-server rate limits. At the configured threshold (default 99%), when ordinary included usage is denied, or when Codex reports a reached limit, the same Omnigent session is relaunched on the next available Codex account.

If a running turn fails with Codex `usageLimitExceeded`, Omni Route rotates and continues on the resumed thread.

If every Codex subscription is exhausted:

- with Claude configured: the same Omnigent session switches to `claude-native-ui`;
- without Claude configured: the session reports that no subscription fallback remains.

## Add accounts later

```bash
omni-rotate accounts
```

Existing profiles are preserved. Add another `codex` profile or configure `claude`, then type `done`.

## Full macOS cleanup

```bash
./uninstall_all.sh
```

For non-interactive use:

```bash
./uninstall_all.sh --yes
```

This removes normal Omnigent CLI/state, Desktop app/data, Omni Route runtime/account state, its PATH blocks, its Homebrew command shim, and this cloned repository when the Git origin is verified.

It intentionally keeps shared tools and unrelated CLIs: Homebrew, Git, `uv`, `tmux`, Node, Codex CLI, and Claude CLI.

## Remove Omni Route only

```bash
./uninstall.sh
```

This removes Omni Route, its compatibility launchers, its PATH blocks and managed command shim, while leaving normal Omnigent and subscription profiles under `~/.omnigent` untouched.
