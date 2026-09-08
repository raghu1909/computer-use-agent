"""Redaction applied to everything that leaves process memory: logs, transcripts,
artifacts, LLM prompts' persisted copies.

Two mechanisms:
1. Known secrets (credential parameter values, API keys) are replaced exactly.
2. Regex classes for regulated identifiers (SSN, card/account/routing numbers).
"""

from __future__ import annotations

import re
from typing import Any

PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("SSN", re.compile(r"\b\d{3}-\d{2}-\d{4}\b")),
    ("CARD", re.compile(r"\b(?:\d[ -]?){13,19}\b")),
    ("ROUTING", re.compile(r"\b(?:routing|aba)[^\d]{0,10}(\d{9})\b", re.I)),
    ("API_KEY", re.compile(r"\bsk-[A-Za-z0-9_-]{10,}\b")),
    ("BEARER", re.compile(r"(?i)bearer\s+[A-Za-z0-9._-]{16,}")),
]


class Redactor:
    def __init__(self, secrets: dict[str, str] | None = None):
        # name -> value; longest values first so partial overlaps redact fully
        self._secrets = sorted(((k, v) for k, v in (secrets or {}).items() if v), key=lambda kv: -len(kv[1]))

    def add_secret(self, name: str, value: str) -> None:
        if value:
            self._secrets.append((name, value))
            self._secrets.sort(key=lambda kv: -len(kv[1]))

    def text(self, s: str) -> str:
        if not isinstance(s, str):
            return s
        for name, value in self._secrets:
            s = s.replace(value, f"[REDACTED:{name}]")
        for label, pat in PATTERNS:
            s = pat.sub(f"[REDACTED:{label}]", s)
        return s

    def obj(self, o: Any) -> Any:
        if isinstance(o, str):
            return self.text(o)
        if isinstance(o, dict):
            return {k: self.obj(v) for k, v in o.items()}
        if isinstance(o, (list, tuple)):
            return [self.obj(v) for v in o]
        return o
