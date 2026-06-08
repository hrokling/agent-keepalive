from __future__ import annotations

from .claude import ClaudeProvider
from .codex import CodexProvider


PROVIDERS = {
    "claude": ClaudeProvider(),
    "codex": CodexProvider(),
}


def get_provider(name: str):
    try:
        return PROVIDERS[name]
    except KeyError as exc:
        raise ValueError(f"unknown provider: {name}") from exc
