# Native Harness Implementation Plan

## Goal

Remove Omnigent from the execution path and keep Codex and Claude fully native.

Omni Route should become only a small supervisor around the native desktop applications. It manages accounts, quota state, switching, app restart/recovery, and the routing dashboard. It must not replace the native conversation UI, skills, plugins, browser/computer use, remote control, or other provider features.

## Fixed product decisions

- Use native Codex Desktop for Codex.
- Use native Claude Desktop / Claude Code for Claude.
- Reuse the existing Omni Route account pool, account priority, cooldown and rotation logic where it still applies.
- Same-provider rotation preserves the exact native session.
- Cross-provider rotation uses a short structured handoff.
- Native provider remote/mobile functionality remains the normal way to control active Codex/Claude work remotely.
- Omni Route may keep a small Tailscale dashboard for routing/account control only.
- Do not manipulate or copy whole provider profile directories. Only the minimum native session state needed to reopen the exact session may be moved if account isolation requires it.

## Runtime model

`omni-route start` starts the small Omni Route supervisor and launches/attaches to the selected native provider.

The supervisor exists only to survive account/provider transitions and native app restarts. It does not own the AI session.

When no Omni Route-controlled work remains active, the supervisor should stop. The dashboard may remain separately available if we choose to keep it always-on.

## Threshold behavior

The switch threshold must be configurable from the Omni Route dashboard.

- User can select/change the switch threshold in the dashboard.
- Maximum allowed switch threshold: **95%**.
- Preparation threshold is always `switch threshold - 3 percentage points`.
- Example: switch at 95%, preparation starts at 92%.

Behavior:

- Below preparation threshold: work normally.
- Preparation range: avoid starting obviously long new work and finish the current safe unit where possible.
- At switch threshold: perform rotation.
- If a hard quota limit happens earlier: recover and rotate immediately using the available state.

## Same-provider rotation

The native session is the anchor.

Example:

```text
Codex account A + native session 123
-> threshold reached
-> select Codex account B
-> make native session 123 available to B if required
-> restart Codex Desktop
-> reopen/resume native session 123
-> verify account B and session 123
-> continue
```

Claude follows the same product behavior.

Required behavior:

1. Remember the exact native session and workspace before switching.
2. Finish/stop the current work at a safe point.
3. Select the next account using the existing rotation rules.
4. Preserve or expose only the exact native session state needed by the new account.
5. Restart the native desktop application.
6. Reopen/resume the exact same native session.
7. Verify that both the expected account and expected session loaded.
8. Only then mark the rotation successful and continue.

If the expected account or session does not load, stop in a visible recovery/error state. Never silently continue on the wrong account or a new empty session.

## Cross-provider rotation

Codex and Claude native sessions are not treated as portable between providers.

Before Codex -> Claude or Claude -> Codex, the current agent creates a short handoff containing only:

- current task/goal;
- current progress;
- important decisions;
- changed/relevant files;
- test/status information;
- blockers;
- exact next action.

Omni Route also preserves the same workspace/repository state.

The other native provider starts in that workspace, reads the newest handoff, verifies the repository/Git state itself, and continues the task.

The handoff should stay concise. The repository and Git state remain the source of truth.

## Failure and edge-case rules

- Never intentionally kill the native app in the middle of an unsafe file/tool operation if a clean boundary is available.
- Rotation must be serialized. Manual and automatic switches must not race each other.
- Persist cooldown/reset state so restart loops cannot bounce back to an exhausted account.
- If all accounts are unavailable, stop and show the reset/login state instead of looping.
- If login/approval is required, stop in a clear `needs user action` state.
- Native browser/computer state is not assumed to transfer across providers.
- A native mobile/remote client may temporarily disconnect during app restart; successful recovery means the native session is usable again afterward.
- Multiple projects/sessions must not overwrite each other's active session identity or recovery state.

## Dashboard

Keep the useful account-routing controls, but the dashboard must no longer behave like an AI client.

It should show/control at least:

- account route/order;
- active provider/account;
- usage/cooldown/reset state;
- configurable switch threshold (max 95%);
- derived preparation threshold;
- current rotation/recovery status;
- manual account/provider switch;
- clear errors or required user action.

## Implementation order

1. **Grill first.** Run one `/grill-me` pass before implementation, maximum 5 questions, focused only on unresolved product decisions. Do not start implementation until the answers are recorded. **Done — answers recorded in [`NATIVE_HARNESS_DECISIONS.md`](NATIVE_HARNESS_DECISIONS.md), which also amends acceptance criteria 3 and 4 below.**
2. **Prove same-provider native reload.** Verify Codex A -> Codex B and Claude A -> Claude B with exact native-session continuation and account verification.
3. **Build the minimal supervisor lifecycle.** Start, monitor, rotate, restart, verify, resume, stop.
4. **Move threshold control into the dashboard.** Enforce max 95% and automatic preparation threshold = switch threshold - 3%.
5. **Add cross-provider handoff.** Codex <-> Claude using the concise structured handoff and the same workspace.
6. **Validate remote behavior.** Native Codex/Claude remote control should remain usable after recovery/restart.
7. **Only then remove Omnigent dependencies.** Do not delete the current working path until the native-first path passes the required flows.

## Acceptance criteria

The native-first rewrite is ready only when:

1. Codex uses the full native Codex desktop experience.
2. Claude uses the full native Claude experience.
3. Codex account rotation restarts and resumes the exact task in the same workspace automatically, via committed state plus handoff, with no user re-explanation.
4. Claude account rotation restarts and resumes the exact task in the same workspace automatically, via committed state plus handoff, with no user re-explanation.
5. The dashboard threshold is user-configurable and cannot exceed 95%.
6. Preparation mode starts automatically 3 percentage points before the configured threshold.
7. Codex <-> Claude can continue the same task through a concise handoff without the user restating it.
8. Wrong-account, missing-session, exhausted-pool and login-required cases fail visibly instead of silently.
9. Native skills, browser/computer use, plugins and remote control are not replaced by Omni Route.
10. Omnigent can be removed without losing account rotation, continuity or routing control.

## Core rule

**Same provider: preserve the exact native session. Different provider: preserve the task through a short handoff. Omni Route only manages lifecycle and accounts.**