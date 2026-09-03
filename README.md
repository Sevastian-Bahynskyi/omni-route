# Omni Route / Omnigent Subscription Rotation v1.1

An isolated patched Omnigent installation for macOS that rotates across your
registered Codex subscription accounts. Claude Pro is an optional final fallback.

It uses official CLI subscription authentication. It does not turn ChatGPT Plus
or Claude Pro into API keys.

## Install

```bash
unzip omnigent-subscription-rotation-v1.1.zip
cd omnigent-subscription-rotation-v1
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
codex     -> sign in account 1
codex     -> sign in account 2
done
```

Later, after getting Claude Pro:

```bash
~/.local/bin/omni-rotate-accounts
```

Then:

```text
claude    -> sign in Claude Pro
done
```

You can rerun the configurator later and add more Codex subscriptions without
re-registering the existing ones.

## Run

```bash
~/.local/bin/omni-rotate codex
```

Your original global `omni` installation and the Omnigent Desktop app are left
untouched.

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
are unlimited. Multi-Claude-account pooling would be a separate runtime feature,
not merely an installer change.

## Uninstall patched copy

```bash
./uninstall.sh
```

This leaves your normal Omnigent installation and registered subscription login
profiles untouched.
