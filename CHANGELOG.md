# Changelog

## 0.3.1

- Replace Claude `--all`'s per-session keeper hierarchy with one multi-root discovery supervisor.
- Monitor local Claude job state independently of authentication and isolate discovery subprocesses from credential variables.
- Qualify Claude targets by config-root fingerprint, preserve unknown activity timestamps, and distinguish discovery, job-state, terminal, blocked, and disappearance outcomes.
- Retain disappeared and terminal observations for a 60-second grace period, then remove them without respawning children.
- Log only lifecycle transitions to four bounded 1 MiB rotating files, including under systemd.
- Add direct, non-login-shell systemd dispatch with credential unsetting, restrictive permissions, and explicit control-group shutdown.

## 0.3.0

- Standardize the public name on `agent-keepalive` across the package, CLI, docs, and service template.
- Add provider architecture with Codex and Claude Code adapters.
- Add Claude Code monitor-only support using `claude agents --json` and `~/.claude/jobs`.
- Add Claude `--all` discovery mode to supervise all live Claude background sessions.
- Remove the old Codex-only compatibility CLI surface.
- Add MIT license, GitHub Actions test workflow, issue templates, and public MVP documentation.
