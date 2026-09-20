"""Append-only JSONL audit log. Every agent action records timestamp, agent,
action, inputs, outputs and the rationale for the decision taken.

Rationale is a required argument on purpose. An entry that cannot say WHY the
agent did something is not an audit entry; it is just a log line.
"""
from __future__ import annotations
import json
import re
import threading
from datetime import datetime, timezone
from typing import Any


class AuditLog:
    def __init__(self, path: str):
        self.path = path
        self._lock = threading.Lock()

    def record(self, agent: str, action: str, *, inputs: Any = None, outputs: Any = None,
               rationale: str, level: str = "info", correlation_id: str | None = None) -> dict:
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "level": level,
            "agent": agent,
            "action": action,
            "correlation_id": correlation_id,
            "inputs": _safe(inputs),
            "outputs": _safe(outputs),
            "rationale": rationale,
        }
        line = json.dumps(entry, default=str)
        with self._lock:
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        return entry

    def read_all(self) -> list[dict]:
        try:
            with open(self.path, encoding="utf-8") as f:
                return [json.loads(l) for l in f if l.strip()]
        except FileNotFoundError:
            return []


_REDACT = ("password", "api_key", "apikey", "token", "secret", "app_password")
_URL_QS = re.compile(r"(\?|&)(key|api_key|token|access_token)=[^&\s]+", re.I)


def scrub(text) -> str:
    """Strip credential-bearing query strings from free text (exception messages, URLs)."""
    return _URL_QS.sub(r"\1\2=***", str(text))


def _safe(obj: Any) -> Any:
    """Redact credentials and coerce pydantic models so nothing sensitive lands on disk."""
    if obj is None:
        return None
    if hasattr(obj, "model_dump"):
        obj = obj.model_dump(mode="json")
    if isinstance(obj, dict):
        return {k: ("***" if any(s in k.lower() for s in _REDACT) else _safe(v)) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_safe(v) for v in obj]
    if isinstance(obj, str):
        return scrub(obj)
    return obj
