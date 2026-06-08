# Contributing

## Scope

`agent-keepalive` is a small host utility. Keep changes narrow, explicit, and easy to reason about.

Prefer:

- provider-specific logic inside `src/agent_keepalive/providers/`
- generic lifecycle logic in the shared runner and state modules
- tests that cover the behavioral change directly

Avoid:

- broad refactors unrelated to the requested behavior
- new external dependencies unless they remove real complexity
- provider features that invent, relaunch, or orchestrate agent sessions unless that behavior is explicitly intended

## Development setup

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -U pip
python -m pip install .
```

Run the CLI locally from source:

```bash
PYTHONPATH=src python3 -m agent_keepalive --help
```

## Tests

Run the unit tests before submitting changes:

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -p 'test_*.py'
```

Build the wheel as a packaging sanity check:

```bash
python3 -m pip wheel . --no-deps
```

## Pull requests

Use focused commits and plain commit messages that describe the behavior change.

For PRs, include:

- what changed
- why it changed
- how it was verified
- any provider-specific limitations or assumptions

## Compatibility policy

The public CLI is `agent-keepalive`.

Backward compatibility is not preserved automatically for experimental or pre-1.0 surfaces. If a change removes or renames a CLI shape, update the README, tests, and service examples in the same change.

When in doubt, prefer the simpler design with the smaller operational surface.
