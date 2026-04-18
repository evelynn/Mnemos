"""TCP reachability probe for stored DB secrets.

Spec §14.2 requires that secrets are validated before they are used.
A full authentication probe would need the language-specific driver
(pyodbc for MSSQL, oracledb for Oracle), which lives inside the analyzer
containers — not in the platform process (spec §7.3/§7.4 keeps the
platform isolated from production DBs).

Phase-A strategy: parse the connection string just enough to extract
``(host, port)`` and attempt a single TCP ``connect()`` with a short
timeout. This catches misconfigured hosts, wrong ports, firewall issues,
and typos at secret-creation time — far better than the previous
decrypt-only stub — while the deeper authentication check happens on
the first analyzer run.
"""

from __future__ import annotations

import asyncio
import re
import socket
from dataclasses import dataclass
from urllib.parse import urlparse

_MSSQL_DEFAULT_PORT = 1433
_ORACLE_DEFAULT_PORT = 1521

# Matches ``Server=host,port`` / ``Data Source=host:port`` / etc. in the
# permissive MSSQL ADO-style connection string.
_MSSQL_HOST_KEYS = ("server", "data source", "address", "addr", "network address")


@dataclass
class ProbeResult:
    ok: bool
    message: str
    host: str | None = None
    port: int | None = None


def parse_endpoint(kind: str, conn: str) -> tuple[str, int] | None:
    """Best-effort ``(host, port)`` parser for MSSQL/Oracle strings.

    Returns ``None`` when the string is unrecognised; the caller then
    records an explicit parse failure rather than blindly attempting TCP.
    """
    conn = conn.strip()

    # URL form works for both.
    if "://" in conn:
        parsed = urlparse(conn)
        if parsed.hostname:
            default = (
                _MSSQL_DEFAULT_PORT if kind == "mssql" else _ORACLE_DEFAULT_PORT
            )
            return parsed.hostname, parsed.port or default

    if kind == "mssql":
        return _parse_mssql_ado(conn)
    if kind == "oracle":
        return _parse_oracle_easyconnect(conn)
    return None


def _parse_mssql_ado(conn: str) -> tuple[str, int] | None:
    for pair in conn.split(";"):
        if "=" not in pair:
            continue
        k, v = pair.split("=", 1)
        if k.strip().lower() in _MSSQL_HOST_KEYS:
            value = v.strip()
            # ``host,port`` or ``host\\instance,port``
            if "," in value:
                host, port = value.rsplit(",", 1)
                try:
                    return host.strip().split("\\", 1)[0], int(port.strip())
                except ValueError:
                    pass
            # ``host:port``
            if ":" in value and not value.startswith("["):
                host, port = value.split(":", 1)
                try:
                    return host.strip(), int(port.strip())
                except ValueError:
                    pass
            return value.split("\\", 1)[0], _MSSQL_DEFAULT_PORT
    return None


def _parse_oracle_easyconnect(conn: str) -> tuple[str, int] | None:
    # ``//host:port/service`` or ``host:port/service`` or ``host/service``.
    m = re.match(r"^/?/?([^:/\s]+)(?::(\d+))?(?:/[^\s]*)?$", conn)
    if not m:
        return None
    host = m.group(1)
    port = int(m.group(2)) if m.group(2) else _ORACLE_DEFAULT_PORT
    return host, port


async def probe_tcp(kind: str, conn: str, timeout: float = 3.0) -> ProbeResult:
    """Attempt a short TCP connect to the parsed endpoint.

    Never raises: all errors are reported via ``ProbeResult.ok=False``.
    """
    parsed = parse_endpoint(kind, conn)
    if parsed is None:
        return ProbeResult(ok=False, message="connection_string_unparseable")
    host, port = parsed

    loop = asyncio.get_running_loop()
    try:
        await asyncio.wait_for(
            loop.run_in_executor(None, _blocking_connect, host, port, timeout),
            timeout=timeout + 1.0,
        )
    except asyncio.TimeoutError:
        return ProbeResult(ok=False, message="timeout", host=host, port=port)
    except OSError as exc:
        return ProbeResult(
            ok=False,
            message=f"unreachable: {exc.__class__.__name__}",
            host=host,
            port=port,
        )

    return ProbeResult(ok=True, message="tcp_reachable", host=host, port=port)


def _blocking_connect(host: str, port: int, timeout: float) -> None:
    sock = socket.create_connection((host, port), timeout=timeout)
    sock.close()
