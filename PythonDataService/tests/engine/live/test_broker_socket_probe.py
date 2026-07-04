from __future__ import annotations

import subprocess

import pytest

from app.engine.live import broker_socket_probe
from app.engine.live.broker_socket_probe import LsofSocketEnumerator


def test_lsof_parser_emits_one_row_per_socket_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        broker_socket_probe,
        "_argv_for_pid",
        lambda pid: ["python", "--run-dir", f"/runs/{pid}"],
    )

    rows = LsofSocketEnumerator()._parse_lsof(
        "\n".join(
            [
                "p21760",
                "cpython",
                "f12",
                "n127.0.0.1:50123->127.0.0.1:4002",
                "TST=ESTABLISHED",
                "f13",
                "n127.0.0.1:50124->127.0.0.1:4002",
                "TST=ESTABLISHED",
            ]
        )
    )

    assert [(row.local_port, row.remote_port) for row in rows] == [
        (50123, 4002),
        (50124, 4002),
    ]
    assert {row.pid for row in rows} == {21760}


def test_enumerator_filters_gateway_side_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = "\n".join(
        [
            "p21760",
            "cpython",
            "f12",
            "n127.0.0.1:50123->127.0.0.1:4002",
            "TST=ESTABLISHED",
            "p900",
            "cjava",
            "f99",
            "n127.0.0.1:4002->127.0.0.1:50123",
            "TST=ESTABLISHED",
        ]
    )

    def fake_run(
        args: list[str],
        **kwargs: object,
    ) -> subprocess.CompletedProcess[str]:
        if args[0] == "lsof":
            assert args[-1] == "pcfnT"
            return subprocess.CompletedProcess(args, 0, stdout=output, stderr="")
        return subprocess.CompletedProcess(
            args,
            0,
            stdout="python -m app.engine.live.run start --run-dir /runs/run-a\n",
            stderr="",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)

    rows = LsofSocketEnumerator().enumerate(4002)

    assert len(rows) == 1
    assert rows[0].pid == 21760
    assert rows[0].local_port == 50123
    assert rows[0].remote_port == 4002
    assert rows[0].run_dir == "/runs/run-a"
