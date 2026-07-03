"""Host-runner broker connection policy."""

from __future__ import annotations

import os
from collections.abc import Mapping

DEFAULT_IBKR_HOST_ALLOWLIST: frozenset[str] = frozenset(
    {
        "127.0.0.1",
        "::1",
        "localhost",
        "host.containers.internal",
        "host.docker.internal",
    }
)


def allowed_ibkr_hosts(environ: Mapping[str, str] | None = None) -> frozenset[str]:
    env = os.environ if environ is None else environ
    configured = {
        host.strip().lower()
        for host in env.get("IBKR_HOST_ALLOWLIST", "").split(",")
        if host.strip()
    }
    env_host = env.get("IBKR_HOST", "").strip()
    if env_host:
        configured.add(env_host.lower())
    return DEFAULT_IBKR_HOST_ALLOWLIST | frozenset(configured)


def validate_ibkr_host_allowed(
    host: str,
    *,
    environ: Mapping[str, str] | None = None,
) -> str:
    lowered = host.lower()
    if lowered not in allowed_ibkr_hosts(environ):
        raise ValueError(
            "ibkr_host is not in the host-daemon allow-list "
            "(IBKR_HOST_ALLOWLIST / IBKR_HOST)"
        )
    return host


__all__ = [
    "DEFAULT_IBKR_HOST_ALLOWLIST",
    "allowed_ibkr_hosts",
    "validate_ibkr_host_allowed",
]
