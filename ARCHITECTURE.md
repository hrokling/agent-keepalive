# Architecture

## Scope

`agent-keepalive` is a small visibility and attachment utility for remote coding-agent sessions. It keeps provider-specific discovery separate from generic JSON state, CLI reporting, and systemd operation. It does not launch replacement sessions, copy transcripts, or act as a general orchestration service.

## Codex

Codex keepers retain the existing one-target attachment model. A keeper connects to the app-server control socket, performs the JSON-RPC handshake, resumes a thread subscription, tracks activity, retries temporary socket unavailability with bounded backoff, and unsubscribes on shutdown. The stale-server version checks and narrowly scoped recovery behavior are unchanged in 0.3.1.

## Claude single-supervisor model

Claude `--bg` work already survives the client connection that launched it. Claude supervision remains useful because it supplies durable discovery and a normalized `list`/`status` view for active, blocked, terminal, invalid, and disappeared jobs across client disconnects.

`agent-keepalive run claude --all` is exactly one long-running Python process. It never starts a persistent keeper for an individual Claude session. Instead it:

1. Accepts one or more Claude config roots.
2. Runs one `claude agents --json` discovery command per root every 20 seconds.
3. Reads `jobs/<short-id>/state.json` under every root once per second, including between CLI discovery polls.
4. Caches the most recent successful discovery entries and typed discovery outcome for each root.
5. Writes inexpensive per-session observation records owned by the supervisor PID.
6. Logs only discovery-outcome and session-lifecycle transitions.

Discovery call count is therefore `configured roots × due discovery polls`, independent of session count. The local-file scan scales with job directories but creates no subprocesses.

### Identity and CLI compatibility

An eight-character Claude ID is not globally unique across config roots. Persisted targets use:

```text
<short-id>@<sha256(canonical-root)>
```

The full source root and original short/full session IDs are also recorded. `status --target <short-id>` remains convenient when exactly one root matches; an ambiguous short ID is rejected with a request to use the root-qualified target. Stopping a supervised session target explicitly stops the shared `claude:all` supervisor because no per-session keeper exists.

### Observation precedence

Claude status uses this precedence:

1. A valid local job state supplies `blocked`, a terminal state, `active`, or another provider state.
2. An existing malformed job file becomes `state_invalid`.
3. A discovery entry without a readable job file becomes `state_missing`.
4. A previously known session absent from both local state and a successful discovery becomes `disappeared`.
5. Failed or invalid discovery never proves disappearance; the last record remains visible while local monitoring continues.

Successful empty discovery, command failure, invalid JSON, missing job state, and invalid job state are separate outcomes. Missing or invalid `updatedAt` values remain unknown; polling time is never substituted for agent activity.

Terminal and disappeared observations remain visible for 60 seconds. They are then removed. An unchanged terminal file is remembered so it is not repeatedly re-admitted; a later non-terminal update makes it visible again.

### Authentication and credential boundary

Monitoring does not call `claude auth status` and does not gate local job-state reads on authentication. Version and discovery subprocesses receive an explicitly constructed environment containing only `HOME`, `PATH`, `LANG`, and the root-specific `CLAUDE_CONFIG_DIR`.

This allowlist excludes Claude, Anthropic, SpreadStation, AWS, Vertex, and other credential variables by construction. In particular, the supervisor neither reads nor passes `CLAUDE_CODE_OAUTH_TOKEN`, `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`, or `SPREADSTATION_CLAUDE_OAUTH_TOKEN_FILE`. Raw discovery stdout/stderr is not stored on failure, and free-form job detail, suggested replies, transcript paths, and provider messages are not copied into keepalive state.

## State and logging

The default root is `~/.local/state/agent-keepalive`:

```text
keepers/*.json   operational keeper/observation records
logs/*.log       direct-start logs only
```

State/log directories are mode 0700 and log files are mode 0600. Each logger uses a 1 MiB active file plus three 1 MiB backups: at most four files and approximately 4 MiB per keeper, including under systemd. The Claude discovery file is named `supervisor-claude.log`, deliberately outside the legacy `claude*.log` cleanup pattern. Unchanged polls produce neither lifecycle logs nor per-session state rewrites. Service bootstrap output and systemd lifecycle messages still go to the user journal.

On graceful Claude supervisor shutdown, all records it owns are removed. Systemd also uses `KillMode=control-group`; there should be no Claude monitor children to kill, but the setting prevents a future regression from escaping the service cgroup.

## systemd model

The template instance remains `agent-keepalive@provider:target.service`. It invokes the installed venv CLI directly through the hidden `service` dispatcher, without a login shell. User lingering keeps enabled services alive across logout and host boot.

The service declares a restrictive umask, a 15-second stop timeout, bounded application logging, explicit credential unsetting, and a fixed executable path under `~/.local/share/agent-keepalive/venv`. Configured Claude roots are supplied by the path-separated `AGENT_KEEPALIVE_CLAUDE_CONFIG_ROOTS` variable.
