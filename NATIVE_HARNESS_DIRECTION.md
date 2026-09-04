# Native Harness Direction

## Why this branch exists

Omni Route was originally built around Omnigent because Omnigent gave us a shared session layer, a mobile/browser client, and a place to coordinate Codex and Claude while rotating subscription accounts.

That tradeoff is becoming less attractive.

Both Codex and Claude now have increasingly capable native harnesses. Those native products are where provider-specific features arrive first: native skills, plugins, MCP integrations, browser/computer interaction, approvals, terminal workflows, desktop integrations, mobile/remote control, and other features that are difficult for a third-party harness to reproduce without lagging behind.

The problem is that our current architecture can make Omnigent the center of the experience instead of Codex and Claude themselves. If that means losing or weakening native capabilities, then Omnigent becomes an unnecessary layer rather than an advantage.

This branch documents the product direction we want to explore. It is intentionally not an implementation plan.

## What we want to achieve

The long-term goal is:

> Keep Codex and Claude as full native harnesses, while Omni Route becomes a thin supervisor responsible only for subscription routing, account rotation, continuity, status, and remote account control.

A user should feel like they are using real Codex when they choose Codex, and real Claude when they choose Claude.

Omni Route should not try to become its own coding harness.

## Native Codex must remain native

When working with Codex, we want the user to retain the normal Codex experience and whatever capabilities OpenAI provides natively.

That includes, where available:

- native Codex UI and workflow;
- native skills and commands;
- native plugins and MCP integrations;
- native browser/computer capabilities;
- native approvals, diffs, tests, terminals and repository interaction;
- native desktop and remote/mobile functionality;
- new Codex features without waiting for Omni Route or another harness to reimplement them.

Omni Route should not replace these features with equivalents of its own.

## Native Claude must remain native

The same applies to Claude.

When working with Claude, we want the actual Claude Code / Claude native experience, including its native skills, agents, hooks, plugins, MCP integrations, terminal workflow, remote-control capabilities and any provider-specific desktop/browser/computer functionality Anthropic exposes.

Again, Omni Route should not become the layer that defines what Claude can or cannot do.

## What Omni Route should own

Omni Route should focus on the capabilities that neither native harness solves well for our use case.

Those are primarily:

- maintaining multiple independent Codex subscription accounts;
- maintaining multiple independent Claude subscription accounts;
- keeping a user-defined priority/order across those accounts;
- detecting when the current account can no longer continue because of subscription limits;
- automatically moving work to the next usable account;
- allowing manual account/provider switching when desired;
- showing account state, current account, cooldown/reset information and routing order;
- preserving enough continuity that a long-running development task can continue without the user repeatedly explaining the same work;
- providing a small remote control surface for routing/account management.

The product should be valuable even if Omnigent disappears entirely.

## What Omni Route should not own

Omni Route should not aim to own:

- the primary coding conversation UI;
- the canonical skill system;
- the browser or computer-use implementation;
- provider-specific plugins;
- the terminal or code-review experience;
- provider-specific agent features;
- a replacement implementation of Codex or Claude functionality.

Whenever possible, those capabilities should remain the responsibility of the native harness.

## Same-provider account rotation

The desired experience for account rotation within one provider is straightforward from a product perspective.

For example:

```text
Codex account A
    -> subscription limit reached
Codex account B
    -> continue the same development task
```

and:

```text
Claude account A
    -> subscription limit reached
Claude account B
    -> continue the same development task
```

The user should not need to start over, manually recreate context, or reconstruct what the agent was doing.

How this continuity is achieved should be proven against the native products rather than assumed here.

## Cross-provider switching

Cross-provider switching is different.

There is no requirement that a Codex conversation and a Claude conversation must literally be the same native conversation object.

The actual product requirement is:

> If Codex can no longer continue, Claude should be able to pick up the same development task with enough context to continue effectively, and vice versa.

The repository, current workspace, Git state, changed files, task objective, important decisions, completed work, test state and remaining work are more important than preserving a provider-independent transcript for its own sake.

We should prefer a reliable handoff between native harnesses over forcing both providers into a shared third-party conversation abstraction if that abstraction reduces native functionality.

## Remote access

Removing Omnigent must not mean losing remote usability.

The desired remote model is:

- the Mac remains the execution host;
- Codex should use the best native remote/mobile mechanism OpenAI provides;
- Claude should use the best native remote/mobile mechanism Anthropic provides;
- Omni Route should provide only the remote surface needed to inspect and control account routing;
- remote access to Omni Route itself should stay secure and private rather than exposing local services directly to the public internet.

A unified mobile conversation UI across Codex and Claude is useful, but it is not worth sacrificing the full native harnesses if provider-native remote clients already solve most of the interaction problem.

## What may be lost by removing Omnigent

Omnigent currently gives us some real benefits that must be consciously evaluated rather than dismissed:

- one provider-independent logical session;
- one transcript visible from one client;
- one shared server that survives provider changes;
- one mobile/browser surface for both providers;
- a convenient place to coordinate cross-provider switching.

The question is not whether these features are useful. They are.

The question is whether they are valuable enough to justify putting another harness between the user and Codex/Claude, especially if doing so restricts native features or creates ongoing compatibility work.

## Success criteria for a native-first Omni Route

A future native-first design should be considered successful if:

1. Using Codex through Omni Route feels materially the same as using native Codex directly.
2. Using Claude through Omni Route feels materially the same as using native Claude directly.
3. Native provider features remain available without Omni Route needing to recreate them.
4. Multiple subscription accounts can still be configured and ordered.
5. Account exhaustion can still result in automatic continuation on another account.
6. Switching providers does not force the user to manually restate the task.
7. Remote use remains practical when the Mac is left running.
8. Omni Route becomes smaller and easier to maintain rather than becoming another general-purpose agent platform.

## Questions that must be answered before removing Omnigent

These are validation questions, not implementation assumptions:

- Can a native Codex session continue cleanly after changing to another isolated Codex subscription account?
- Can a native Claude session continue cleanly after changing to another isolated Claude subscription account?
- What happens to each provider's native remote/mobile connection when the active subscription account changes?
- What minimum handoff information is required for a reliable Codex-to-Claude or Claude-to-Codex continuation?
- Which useful Omnigent behaviors would genuinely have to be rebuilt, and which are already better handled by the native providers?

These questions should be answered experimentally before committing to a rewrite.

## Direction

The preferred direction to investigate is therefore:

```text
Native Codex        Native Claude
      \                /
       \              /
        Omni Route supervisor

        account pool
        quota state
        automatic rotation
        provider handoff
        routing dashboard
        remote account control
```

Omni Route should become infrastructure around the native harnesses, not the harness through which the user experiences them.

This branch exists to evaluate that direction before changing the current working system.