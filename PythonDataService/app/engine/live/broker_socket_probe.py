"""Host-side IBKR Gateway socket probe."""

from __future__ import annotations

import shlex
import subprocess

from fastapi import status

from app.schemas.broker_session import GatewaySocketRow

_LSOF_TIMEOUT_SECONDS = 5.0
_PS_TIMEOUT_SECONDS = 2.0


class BrokerSocketProbeError(RuntimeError):
    """Socket probe failure that should surface as a daemon HTTP response."""

    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


class LsofSocketEnumerator:
    """Enumerate ESTABLISHED TCP sockets touching the IBKR Gateway port."""

    def enumerate(self, gateway_port: int) -> list[GatewaySocketRow]:
        try:
            proc = subprocess.run(
                [
                    "lsof",
                    "-nP",
                    f"-iTCP:{gateway_port}",
                    "-sTCP:ESTABLISHED",
                    "-F",
                    "pcnT",
                ],
                capture_output=True,
                text=True,
                timeout=_LSOF_TIMEOUT_SECONDS,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise BrokerSocketProbeError(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                f"could not enumerate IBKR gateway sockets: {exc}",
            ) from exc
        if proc.returncode not in (0, 1):
            detail = (
                proc.stderr.strip()
                or proc.stdout.strip()
                or f"lsof exited {proc.returncode}"
            )
            raise BrokerSocketProbeError(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                f"could not enumerate IBKR gateway sockets: {detail}",
            )
        return self._parse_lsof(proc.stdout)

    def _parse_lsof(self, output: str) -> list[GatewaySocketRow]:
        rows: list[GatewaySocketRow] = []
        current: dict[str, object] | None = None
        for raw_line in output.splitlines():
            if not raw_line:
                continue
            tag = raw_line[0]
            value = raw_line[1:]
            if tag == "p":
                if current is not None:
                    rows.append(self._row_from_record(current))
                current = {"pid": _safe_int(value)}
                continue
            if current is None:
                continue
            if tag == "c":
                current["command"] = value
            elif tag == "n":
                local_port, remote_host, remote_port = _parse_lsof_name(value)
                current["local_port"] = local_port
                current["remote_host"] = remote_host
                current["remote_port"] = remote_port
            elif tag == "T" and value == "ST=ESTABLISHED":
                current["state"] = "ESTABLISHED"
        if current is not None:
            rows.append(self._row_from_record(current))
        return rows

    def _row_from_record(self, record: dict[str, object]) -> GatewaySocketRow:
        pid = record.get("pid")
        argv = _argv_for_pid(pid) if isinstance(pid, int) else []
        return GatewaySocketRow(
            pid=pid if isinstance(pid, int) else None,
            command=str(record.get("command") or ""),
            argv=argv,
            run_dir=_run_dir_from_argv(argv),
            local_port=record.get("local_port")
            if isinstance(record.get("local_port"), int)
            else None,
            remote_host=record.get("remote_host")
            if isinstance(record.get("remote_host"), str)
            else None,
            remote_port=record.get("remote_port")
            if isinstance(record.get("remote_port"), int)
            else None,
        )


def _safe_int(value: str) -> int | None:
    try:
        return int(value)
    except ValueError:
        return None


def _parse_lsof_name(name: str) -> tuple[int | None, str | None, int | None]:
    local, separator, remote = name.partition("->")
    if not separator:
        return _port_from_endpoint(local), None, None
    remote_host, remote_port = _host_port_from_endpoint(remote)
    return _port_from_endpoint(local), remote_host, remote_port


def _host_port_from_endpoint(endpoint: str) -> tuple[str | None, int | None]:
    if endpoint.startswith("["):
        host, separator, tail = endpoint.rpartition("]:")
        if separator:
            return host.removeprefix("["), _safe_int(tail)
    host, separator, port = endpoint.rpartition(":")
    if not separator:
        return endpoint or None, None
    return host or None, _safe_int(port)


def _port_from_endpoint(endpoint: str) -> int | None:
    return _host_port_from_endpoint(endpoint)[1]


def _argv_for_pid(pid: object) -> list[str]:
    if not isinstance(pid, int):
        return []
    try:
        proc = subprocess.run(
            ["ps", "-p", str(pid), "-o", "args="],
            capture_output=True,
            text=True,
            timeout=_PS_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if proc.returncode != 0:
        return []
    try:
        return shlex.split(proc.stdout.strip())
    except ValueError:
        return []


def _run_dir_from_argv(argv: list[str]) -> str | None:
    for index, value in enumerate(argv):
        if value == "--run-dir" and index + 1 < len(argv):
            return argv[index + 1]
        prefix = "--run-dir="
        if value.startswith(prefix):
            return value[len(prefix):]
    return None
