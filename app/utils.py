import json
from datetime import datetime, timezone
from typing import Any


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_json(raw: str, fallback: Any) -> Any:
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return fallback


class _SafeDict(dict):
    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


def safe_format(template: str, **kwargs: Any) -> str:
    """Format template with kwargs; unknown placeholders are left as-is."""
    try:
        return template.format_map(_SafeDict(**{k: str(v) for k, v in kwargs.items()}))
    except (ValueError, KeyError):
        return template
