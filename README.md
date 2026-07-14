# agent-keepalive

`agent-keepalive` keeps remote coding-agent sessions visible and attached on long-lived hosts.

It currently supports:

- **Codex**: attaches to the local app-server control socket and stays subscribed to a thread until a configurable idle timeout.
- **Claude Code**: monitors Claude Code background session state through Claude's own CLI and job files until the session becomes terminal or idle.

The project is intentionally small. It does not patch Codex or Claude Code, does not rebuild either tool, does not require tmux, and does not copy conversation history into its own state.

## Stability warning

This tool relies on local implementation details of coding-agent CLIs:

- Codex app-server websocket-over-unix-socket JSON-RPC methods.
- Claude Code `claude agents --json` output and `~/.claude/jobs/<short-id>/state.json`.

Those interfaces may change without notice. Treat this as a pragmatic host utility, not a stable vendor API client.

## Install

Supported Python versions: `3.11` through `3.14`.

From the repo root:

```bash
python3 -m venv ~/.local/share/agent-keepalive/venv
~/.local/share/agent-keepalive/venv/bin/python -m pip install .
```

For development:

```bash
PYTHONPATH=src python3 -m agent_keepalive --help
```

The primary CLI is:

```bash
agent-keepalive --help
```

## Codex usage

Start a background keeper for the most recent loaded or recent Codex thread:

```bash
agent-keepalive start codex --last --idle-timeout 1h
```

Start a background keeper for a specific Codex thread:

```bash
agent-keepalive start codex --thread 019e7526-3336-7c41-84ba-c566efdd199b --idle-timeout 90m
```

If the Codex control socket is not at the default path, pass it explicitly:

```bash
agent-keepalive start codex \
  --thread 019e7526-3336-7c41-84ba-c566efdd199b \
  --socket ~/.codex/app-server-control/desktop-ssh-websocket-v0.sock
```

Codex keeps a thread alive by:

1. Connects to the local Codex app-server control socket.
2. Probes the target socket and runs a Codex app-server version preflight before it selects or resumes a thread.
3. If the socket is serving an older app-server than the local Codex CLI / managed version, it tries a safe recovery first.
4. Performs the normal `initialize` + `initialized` handshake.
5. Calls `thread/resume` to attach as a subscriber.
6. Tracks thread activity and status notifications.
7. Calls `thread/unsubscribe` and exits after the configured idle timeout.

Codex stale-server recovery works like this:

1. Probes the exact target socket with the normal `initialize` handshake and extracts the running app-server version.
2. Runs `codex app-server daemon version` and parses the JSON output.
3. If the socket is current, keepalive attaches normally.
4. If the socket is stale, keepalive first tries `codex app-server daemon restart`.
5. If restart fails because an unmanaged `codex ... app-server --listen` process is holding the daemon socket, keepalive finds the process that owns that exact unix-socket inode, verifies that its command line matches a Codex app-server listener, stops only that process, and retries the daemon restart.
6. If the socket still does not come back on the expected version, or if the process/socket ownership is ambiguous, keepalive exits with a clear error instead of attaching to stale state.

Limitations:

- Automatic recovery only applies to the daemon-managed Codex socket. If you point keepalive at a different custom socket and it is stale, keepalive fails loudly instead of trying to restart an unrelated server.
- The unmanaged fallback assumes the stale listener is a `codex ... app-server --listen` process for the exact socket path. If that assumption cannot be proved from `/proc`, recovery is refused.

If a systemd-managed Codex keeper starts before the app-server socket exists or while it refuses connections, the keeper remains running, records a clear diagnostic, and retries with exponential backoff from 5 seconds up to 5 minutes. Other startup failures still exit normally so systemd can report or restart them.

## Claude Code usage

Discover and supervise all live Claude Code background sessions:

```bash
agent-keepalive start claude --all --idle-timeout 1h
```

Limit discovery to one repository:

```bash
agent-keepalive start claude --all --cwd /path/to/repo --idle-timeout 1h
```

Monitor one specific Claude Code background session:

```bash
agent-keepalive start claude --session 232e2060 --cwd /path/to/repo
```

Claude sessions can be identified by full UUID or the 8-character short id used under `~/.claude/jobs`.

Claude monitoring works by:

1. Runs preflight checks with `claude --version`, `claude auth status --text`, and `claude agents --json`.
2. In `--all` mode, polls `claude agents --json` and starts one keeper per live Claude session it discovers.
3. For each discovered or explicitly targeted session, reads `~/.claude/jobs/<short-id>/state.json`.
4. Records status, detail, tempo, needs, suggested reply, transcript path, auth state, and cwd.
5. Exits when a Claude job is terminal, or after the configured idle timeout for non-active states.

Claude support is monitor-only. It discovers and adopts live Claude sessions, but it does not invent or relaunch missing ones.

## Common commands

List active keepers:

```bash
agent-keepalive list
agent-keepalive list --provider codex
agent-keepalive list --provider claude
```

Inspect a keeper:

```bash
agent-keepalive status --provider codex --target 019e7526-3336-7c41-84ba-c566efdd199b
agent-keepalive status --provider claude --target 232e2060
```

Stop keepers:

```bash
agent-keepalive stop --provider codex --target 019e7526-3336-7c41-84ba-c566efdd199b
agent-keepalive stop --all
```

Example `list` output:

```text
PROVIDER  TARGET                                 PID    STATUS      LOADED  LAST ACTIVITY   IDLE TIMEOUT  NAME
codex     019e7526-3336-7c41-84ba-c566efdd199b   81234  active      yes     12s ago         1h            Activate on buildserver
claude    all                                    81298  active      yes     8s ago          1h            Claude discovery
claude    232e2060                               81341  blocked     yes     41s ago         1h            spreadstation
```

Example `status` output:

```text
Provider: claude
Target: 232e2060
Name: spreadstation
PID: 81341
Keeper status: attached
Target status: blocked
Loaded: yes
Blocked: True
Terminal: False
Idle timeout: 1h
Provider metadata:
  claude_bin: claude
  cwd: /srv/work/spreadstation
  session_id: 232e2060-1111-2222-3333-444455556666
```

## State and privacy

By default the tool writes:

- State: `~/.local/state/agent-keepalive/keepers/*.json`
- Logs: `~/.local/state/agent-keepalive/logs/*.log`

State files contain operational metadata only: provider, target id, pid, display name, status, timestamps, idle timeout, log path, and provider-specific paths/status fields.

No conversation history is copied into `agent-keepalive` state. Claude transcript paths may be recorded as paths, but transcript contents are not ingested by this tool.

## systemd user service

Install the template unit:

```bash
mkdir -p ~/.config/systemd/user
cp systemd/agent-keepalive@.service ~/.config/systemd/user/
systemctl --user daemon-reload
```

Start and enable a Codex keeper:

```bash
systemctl --user start 'agent-keepalive@codex:019e7526-3336-7c41-84ba-c566efdd199b.service'
systemctl --user enable 'agent-keepalive@codex:019e7526-3336-7c41-84ba-c566efdd199b.service'
```

Start and enable a Claude Code discovery supervisor:

```bash
systemctl --user start 'agent-keepalive@claude:all.service'
systemctl --user enable 'agent-keepalive@claude:all.service'
```

For Claude Code, use a drop-in to scope discovery to one repository if desired:

```ini
[Service]
Environment=AGENT_KEEPALIVE_CLAUDE_CWD=/path/to/repo
Environment=AGENT_KEEPALIVE_IDLE_TIMEOUT=1h
```

For Codex socket overrides:

```ini
[Service]
Environment=AGENT_KEEPALIVE_PROVIDER=codex
Environment=AGENT_KEEPALIVE_CODEX_SOCKET=/home/you/.codex/app-server-control/desktop-ssh-websocket-v0.sock
Environment=AGENT_KEEPALIVE_IDLE_TIMEOUT=1h
```

When a systemd-managed Codex keeper starts before the app-server is ready, it waits and reruns the same preflight before reattaching. This avoids a restart loop during boot while preserving automatic recovery and ensuring a local Codex CLI upgrade does not leave the keeper bound to an older app-server indefinitely.

To allow user services to run after logout and start without an interactive login:

```bash
loginctl enable-linger "$USER"
```

## Development

Run tests:

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -p 'test_*.py'
```

Build a wheel:

```bash
python3 -m pip wheel . --no-deps
```

Other docs:

- [ARCHITECTURE.md](ARCHITECTURE.md): provider model and process layout
- [CONTRIBUTING.md](CONTRIBUTING.md): contribution expectations
- [RELEASING.md](RELEASING.md): publish and release checklist
