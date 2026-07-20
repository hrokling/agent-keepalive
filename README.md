# agent-keepalive

`agent-keepalive` keeps remote coding-agent sessions visible and attached on long-lived hosts.

- **Codex:** attaches to the local app-server socket and stays subscribed to a thread until a configurable idle timeout.
- **Claude Code:** runs one discovery supervisor that reports background jobs across one or more Claude config roots. Claude `--bg` execution is already independent; keepalive adds durable discovery, normalized blocked/terminal/disappeared status, and one buildserver-side `list`/`status` control plane across MacBook sleeps and SSH disconnects.

The project is intentionally small. It does not patch either provider, launch replacement sessions, require tmux, copy conversations, or ingest transcripts.

## Stability warning

This utility relies on local provider implementation details: Codex app-server JSON-RPC and Claude Code's `claude agents --json` plus `jobs/<short-id>/state.json`. Those interfaces may change without notice.

## Install

Python 3.11 through 3.14 is supported. The documented host installation uses a dedicated venv:

```bash
python3 -m venv ~/.local/share/agent-keepalive/venv
python3 -m pip wheel . --no-deps --wheel-dir /tmp/agent-keepalive-wheel
~/.local/share/agent-keepalive/venv/bin/python -m pip install --upgrade \
  /tmp/agent-keepalive-wheel/agent_keepalive-*.whl
~/.local/share/agent-keepalive/venv/bin/agent-keepalive --version
```

For development:

```bash
PYTHONPATH=src python3 -m agent_keepalive --help
```

## Codex usage

Start a background keeper for a recent or specific thread:

```bash
agent-keepalive start codex --last --idle-timeout 1h
agent-keepalive start codex --thread THREAD_ID --idle-timeout 90m
```

Override the app-server socket when needed:

```bash
agent-keepalive start codex --thread THREAD_ID \
  --socket ~/.codex/app-server-control/desktop-ssh-websocket-v0.sock
```

Codex performs a version preflight, attaches with the normal initialize/resume handshake, tracks thread status/activity, and unsubscribes after the configured idle timeout. Temporary socket unavailability retries from 5 seconds to 5 minutes. For the daemon-managed socket only, a stale app-server is restarted; an unmanaged listener is stopped only when its exact socket ownership and Codex command line can be proved. Ambiguous recovery fails safely.

## Claude usage

Run the single supervisor for the default root:

```bash
agent-keepalive start claude --all
```

Observe both an interactive root and an isolated automation root:

```bash
agent-keepalive start claude --all \
  --config-root ~/.claude \
  --config-root ~/.config/spreadstation/claude-opus-runtime
```

An optional `--cwd /repo` filters each root's CLI discovery. A single-session monitor remains available for compatibility:

```bash
agent-keepalive start claude --session 232e2060 --cwd /repo \
  --config-root ~/.claude
```

### How Claude supervision works

`--all` is one long-running supervisor and has zero persistent per-session keeper children. Every second it reads local `jobs/<short-id>/state.json` files. Every 20 seconds it runs at most one `claude agents --json` command per configured root and caches the typed outcome. Discovery work therefore scales with roots and polling intervals, not session count.

Local state monitoring is authentication-independent. Normal monitoring never calls `claude auth status`. Claude subprocesses receive only `HOME`, `PATH`, `LANG`, and the current `CLAUDE_CONFIG_DIR`; credential variables are neither inherited nor stored. The dedicated SpreadStation token file is never opened by keepalive.

Session targets are root-qualified because the same short ID can exist in different roots:

```text
232e2060@<source-root-sha256>
```

The suffix is the full SHA-256 digest of the canonical source-root path, so roots cannot collide through digest truncation. `status --target 232e2060` works when the short ID is unique; use the full root-qualified target copied from `list` when it is ambiguous.

Claude lifecycle states are deliberately distinct:

- `state_missing`: discovery saw the session but its job file is absent;
- `state_invalid`: the job file is unreadable, malformed, or structurally invalid;
- `blocked`: the job is waiting for a prerequisite or response;
- provider terminal states such as `done`, `failed`, or `cancelled`;
- `disappeared`: a previously known session is absent from both local state and a successful discovery.

Failed discovery and invalid discovery JSON are recorded on the supervisor and do not masquerade as successful empty discovery. They also do not make sessions disappear. Terminal and disappeared records remain visible for 60 seconds and are then removed. Unknown activity remains `-`; poll time is never presented as provider activity.

The old `--all` idle timeout is accepted for CLI compatibility but does not terminate observations. Claude `--bg` owns execution lifetime; keepalive owns visibility.

## List, status, and stop

```bash
agent-keepalive list
agent-keepalive list --provider claude
agent-keepalive status --provider claude --target '232e2060@SOURCE_ROOT_SHA256'
agent-keepalive stop --provider claude --target all
agent-keepalive stop --all
```

Example Claude rows:

```text
PROVIDER  TARGET                                PID  STATUS       LOADED  LAST ACTIVITY  IDLE TIMEOUT  NAME
claude    all                                 81298  active       yes     -              0s            Claude discovery supervisor
claude    232e2060@SOURCE_ROOT_SHA256       81298  blocked      yes     41s ago        0s            232e2060-...
```

All Claude session observations share the supervisor PID. Stopping one root-qualified observation explicitly reports that it is stopping the shared `claude:all` supervisor.

## State, privacy, and bounded logs

Default paths:

```text
~/.local/state/agent-keepalive/keepers/*.json
~/.local/state/agent-keepalive/logs/*.log
```

State includes operational IDs, paths, normalized status, timestamps, root identity, and discovery outcome. It excludes conversation text, free-form job detail, suggested replies, transcript paths/content, subprocess output, authentication data, and credential values.

Transition logs always use bounded rotation: one 1 MiB active file plus three 1 MiB backups, approximately 4 MiB maximum per keeper. The Claude supervisor writes `supervisor-claude.log`, which is intentionally outside the legacy `claude*.log` cleanup pattern. Service/bootstrap messages still appear in journald. Only lifecycle transitions, discovery outcome changes, warnings, and failures are logged; unchanged polls are silent.

## systemd user service

Install the merged template and reload:

```bash
install -m 600 systemd/agent-keepalive@.service \
  ~/.config/systemd/user/agent-keepalive@.service
systemctl --user daemon-reload
loginctl enable-linger "$USER"
```

For a multi-root Claude supervisor, create this drop-in:

```ini
# ~/.config/systemd/user/agent-keepalive@claude:all.service.d/override.conf
[Service]
Environment=AGENT_KEEPALIVE_CLAUDE_BIN=claude
Environment=AGENT_KEEPALIVE_CLAUDE_CONFIG_ROOTS=/home/you/.claude:/home/you/.config/spreadstation/claude-opus-runtime
```

Do not add Claude, Anthropic, SpreadStation, AWS, or Vertex credentials. The template explicitly unsets the known Claude/Anthropic/SpreadStation credential variables, and each discovery subprocess uses its own minimal allowlist.

Enable and inspect the service:

```bash
systemctl --user enable --now 'agent-keepalive@claude:all.service'
systemctl --user is-enabled 'agent-keepalive@claude:all.service'
systemctl --user is-active 'agent-keepalive@claude:all.service'
journalctl --user -u 'agent-keepalive@claude:all.service' --since=-10m
```

The unit invokes the venv CLI directly without a login shell, uses mode-0077-created files and bounded rotation, journals bootstrap output, stops the whole control group, and allows 15 seconds for graceful record cleanup.

For Codex, enable an instance such as:

```bash
systemctl --user enable --now 'agent-keepalive@codex:THREAD_ID.service'
```

Use a drop-in to override `AGENT_KEEPALIVE_CODEX_SOCKET` when needed.

## Troubleshooting

- **`failure` discovery with local sessions still visible:** this is expected degradation. Check the user journal and run the same root with a credential-free environment; local job state remains authoritative.
- **`invalid_json`:** the installed Claude CLI returned a non-array or malformed payload. Upgrade Claude Code or temporarily rely on local job files.
- **`state_missing`:** discovery is ahead of job-file creation or cleanup. The next local poll updates it.
- **`state_invalid`:** inspect the named root's job file permissions/shape without copying its content into an issue.
- **`disappeared`:** both a successful discovery and the local root stopped seeing the session. The record remains for 60 seconds.
- **Ambiguous short ID:** copy the root-qualified target from `agent-keepalive list`.
- **Service restart loop:** keep the Claude service disabled, inspect `journalctl --user -u ...` and `~/.local/state/agent-keepalive/logs/supervisor-claude.log`, verify the installed CLI/version and root paths, then restart only this service.

## Rollback

To contain Claude supervision without affecting Claude `--bg` execution or Codex keepers:

```bash
systemctl --user disable --now 'agent-keepalive@claude:all.service'
```

Reinstall the previously retained wheel into `~/.local/share/agent-keepalive/venv`, restore its matching unit template/drop-in, run `systemctl --user daemon-reload`, and re-enable only after verifying the older behavior is acceptable. If no safe prior build is available, leave the Claude service disabled; background Claude jobs continue independently and can be inspected with Claude's own CLI. Never roll back by adding credentials to the supervisor environment.

## Development and release checks

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -p 'test_*.py'
python3 -m compileall -q src tests
python3 -m pip wheel . --no-deps
```

See [ARCHITECTURE.md](ARCHITECTURE.md), [CONTRIBUTING.md](CONTRIBUTING.md), and [RELEASING.md](RELEASING.md).
