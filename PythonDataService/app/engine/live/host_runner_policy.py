"""Host-runner broker connection policy."""

from __future__ import annotations

import os
from collections.abc import Mapping, MutableMapping
from pathlib import Path

DEFAULT_IBKR_HOST_ALLOWLIST: frozenset[str] = frozenset(
    {
        "127.0.0.1",
        "::1",
        "localhost",
        "host.containers.internal",
        "host.docker.internal",
    }
)
_CONTAINER_HOST_ALIASES: frozenset[str] = frozenset(
    {
        "host.containers.internal",
        "host.docker.internal",
    }
)
IBKR_HOST_POLICY_ENV_KEYS: tuple[str, ...] = (
    "IBKR_HOST_ALLOWLIST",
    "IBKR_HOST",
    "LIVE_RUNNER_IBKR_CLIENT_ID_POOL",
)


def load_policy_env_file(
    env_file: str | Path,
    *,
    environ: MutableMapping[str, str] | None = None,
    missing_ok: bool = True,
) -> tuple[str, ...]:
    """Load daemon-owned IBKR policy keys from a dotenv-style file.

    The host daemon needs only its connection policy and child client-id pool,
    not the whole application settings surface. Keep this narrow and non-executable:
    ``.env`` is parsed as data, never sourced by a shell.
    """
    target = os.environ if environ is None else environ
    path = Path(env_file)
    if not path.exists():
        if missing_ok:
            return ()
        raise FileNotFoundError(path)

    loaded: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        parsed = _parse_policy_env_line(line)
        if parsed is None:
            continue
        key, value = parsed
        if key not in IBKR_HOST_POLICY_ENV_KEYS or key in target:
            continue
        target[key] = value
        loaded.append(key)
    return tuple(loaded)


def _parse_policy_env_line(line: str) -> tuple[str, str] | None:
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        return None
    if stripped.startswith("export "):
        stripped = stripped.removeprefix("export ").lstrip()
    key, separator, raw_value = stripped.partition("=")
    if not separator:
        return None
    key = key.strip()
    value = raw_value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        value = value[1:-1]
    else:
        value = value.split(" #", 1)[0].strip()
    return key, value


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


def host_process_ibkr_host(host: str) -> str:
    """Translate a container-to-host alias for a host-native broker client.

    The data plane reaches the local Gateway through a container alias, but
    the daemon launches Clerks and bots directly on that same host. Those
    child processes must use host loopback; the container alias may not exist
    in host DNS at all (notably with Podman on macOS).
    """

    if host.strip().lower() in _CONTAINER_HOST_ALIASES:
        return "127.0.0.1"
    return host


__all__ = [
    "DEFAULT_IBKR_HOST_ALLOWLIST",
    "IBKR_HOST_POLICY_ENV_KEYS",
    "allowed_ibkr_hosts",
    "host_process_ibkr_host",
    "load_policy_env_file",
    "validate_ibkr_host_allowed",
]
