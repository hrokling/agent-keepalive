# Architecture

## Overview

`agent-keepalive` is a small supervisor for long-lived coding-agent sessions on remote hosts.

It separates:

- provider-specific session discovery and status inspection
- generic keeper lifecycle, state persistence, and reporting
- systemd integration for boot-time and post-logout operation

## Components

### CLI

`src/agent_keepalive/cli.py` is the public entrypoint.

It is responsible for:

- parsing `start`, `run`, `list`, `status`, and `stop`
- resolving provider targets
- spawning background keepers
- rendering operator-facing status

### Provider registry

`src/agent_keepalive/providers/__init__.py` dispatches named providers.

Each provider adapter implements the provider-specific parts:

- resolve a target from CLI arguments
- produce live status for stored keeper records
- translate provider-native state into a shared operational model

### Generic keeper

`src/agent_keepalive/runner.py` runs one foreground keeper for one provider target.

Shared keeper responsibilities:

- create and update a state record
- persist operational metadata under `~/.local/state/agent-keepalive`
- apply idle-timeout logic
- expose a consistent `list` and `status` view regardless of provider

### Claude discovery supervisor

`src/agent_keepalive/claude_supervisor.py` is a provider-specific helper for `claude --all`.

It does not monitor one Claude session directly. Instead it:

- polls `claude agents --json`
- filters to live sessions, optionally by working directory
- starts one normal Claude keeper per discovered session
- leaves per-session state handling to the generic keeper path

This keeps `--all` as a thin discovery layer rather than a second session-management system.

## Provider model

### Codex

The Codex provider attaches to the local app-server control socket and keeps a target thread subscribed.

Key dependencies:

- websocket-over-unix-socket transport
- Codex JSON-RPC methods such as `thread/resume` and `thread/unsubscribe`
- `codex app-server daemon version` for daemon-managed version checks and stale-server recovery

Before selecting or resuming a thread, the Codex path verifies that the app-server behind the target socket is on the expected local Codex version. If the daemon socket is stale, it first tries a normal daemon restart and only falls back to stopping a single unmanaged `codex ... app-server --listen` listener when that exact process can be proven to own the stale socket. If recovery is ambiguous or unsuccessful, the provider fails loudly instead of attaching to stale server state.

### Claude Code

The Claude provider is monitor-only.

It inspects:

- `claude --version`
- `claude auth status --text`
- `claude agents --json`
- `~/.claude/jobs/<short-id>/state.json`

It discovers and adopts live Claude sessions. It does not invent or relaunch missing sessions.

That boundary is deliberate. Session creation belongs to the provider CLI; `agent-keepalive` is responsible for supervision and visibility once a session exists.

## State layout

Default state root:

```text
~/.local/state/agent-keepalive/
```

Contents:

- `keepers/*.json`: persisted keeper records
- `logs/*.log`: stdout/stderr for background keepers

The stored data is operational metadata only. Transcript paths may be referenced for Claude sessions, but transcript contents are not ingested.

## systemd model

The template unit is `systemd/agent-keepalive@.service`.

Instance naming follows `provider:target`, for example:

- `codex:<thread-id>`
- `claude:all`

The service starts the foreground CLI form and relies on:

- `Restart=on-failure`
- user lingering for boot/logout independence
- environment overrides for provider-specific settings

The overall bias is simple: one small process per tracked target, plain JSON state on disk, and no attempt to become a full orchestration platform.
