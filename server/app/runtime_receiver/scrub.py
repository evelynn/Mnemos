"""Scrub spans before they land in storage (§7.6)."""

from __future__ import annotations

import re
from typing import Any

from app.data_sampler.masking import MaskingEngine

_engine = MaskingEngine()

_SENSITIVE_ATTRS = re.compile(
    r"(?i)(http\.request\.body|http\.response\.body|db\.statement|auth|password|token)"
)
_SQL_LITERAL = re.compile(r"'[^']*'")


def _scrub_value(key: str, value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, (int, float, bool)):
        return value
    text = str(value)
    if _SENSITIVE_ATTRS.search(key):
        if key.lower().endswith("db.statement"):
            return _SQL_LITERAL.sub("'?'", text)
        return "[SCRUBBED]"
    scrubbed, _ = _engine.mask_value(key, text)
    return scrubbed


def scrub_span(span: dict[str, Any]) -> dict[str, Any]:
    cleaned = dict(span)
    attrs = cleaned.get("attributes") or {}
    if isinstance(attrs, dict):
        cleaned["attributes"] = {k: _scrub_value(k, v) for k, v in attrs.items()}
    elif isinstance(attrs, list):
        cleaned["attributes"] = [
            {**a, "value": _scrub_value(a.get("key", ""), a.get("value"))}
            for a in attrs
        ]
    return cleaned
