"""Capability scopes — the vocabulary skills use to declare what their tools can touch.

Scopes are the middle layer between "this tool exists" and "this tool may run in
this context." A skill declares the scopes its tools need; the orchestrator
records which scope was exercised on every dispatch so future policy (budget
caps, allowlists, per-skill auth) has a stable identifier to hang off.

Add new scopes here sparingly — a large enum of fine-grained scopes is harder to
reason about than a small one of coarse scopes. The current set deliberately
mirrors the two resource surfaces that exist today (Obsidian vault, filevault)
plus the one action surface (sending a file back through Telegram).
"""

from __future__ import annotations

from enum import Enum


class Scope(str, Enum):
    VAULT_READ = "vault:read"
    VAULT_WRITE = "vault:write"
    FILEVAULT_READ = "filevault:read"
    FILEVAULT_WRITE = "filevault:write"
    FILEVAULT_SEND = "filevault:send"
    WEB_SEARCH = "web:search"
    WEB_FETCH = "web:fetch"

    def __str__(self) -> str:
        return self.value
