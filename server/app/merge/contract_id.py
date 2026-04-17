"""Contract ID normalisation (spec §5.1.Contract).

The first analyzers emit IDs in each family's shape, but cross-language
matching only works if both sides agree on the canonical form. This module
is the single source of truth — analyzers call into it (via Python) or mirror
the rules (C#/TS). The Week-4 merge step rewrites any ``http_endpoint``
node IDs to the canonical form so the C# controller and the TS ``fetch`` land
on the same node in the graph.
"""

from __future__ import annotations

import re

_PATH_VAR = re.compile(r"[{:][^}/]*}?")
_MULTI_SLASH = re.compile(r"/+")


def normalize_http_path(raw: str) -> str:
    """Strip querystrings and collapse path variables into ``{name}`` slots.

    Examples:
        /api/orders/{id}      → /api/orders/{id}
        /api/orders/:id       → /api/orders/{id}
        /api/orders/123?x=1   → /api/orders/{id?}
    """
    path = raw.split("?", 1)[0]
    if not path.startswith("/"):
        path = "/" + path
    # Route templates: /:name  →  /{name}
    path = re.sub(r"/:([A-Za-z0-9_]+)", r"/{\1}", path)
    # Literal numeric segments → {id}
    path = re.sub(r"/\d+(?=(/|$))", "/{id}", path)
    path = _MULTI_SLASH.sub("/", path)
    if path.endswith("/") and len(path) > 1:
        path = path[:-1]
    return path


def http_contract_id(method: str, raw_path: str) -> str:
    method = method.upper().strip()
    return f"http.{method}.{normalize_http_path(raw_path)}"


def grpc_contract_id(service_fqn: str, method: str) -> str:
    return f"grpc.{service_fqn}.{method}"


def mq_contract_id(kind: str, topic: str) -> str:
    return f"mq.{kind}.{topic}"


def dll_export_id(assembly: str, type_fqn: str, signature: str) -> str:
    return f"dll.{assembly}.{type_fqn}.{signature}"


def file_drop_id(path_pattern: str) -> str:
    return f"file.drop.{path_pattern}"
