"""Regression tests for the fleet stress tooling in this directory.

Stdlib-only (unittest + unittest.mock), mirroring the tools' own
zero-dependency design and ``scripts/test_check_adr_status.py``: the CI job
that runs them installs nothing but Python itself.

Run directly: ``python scripts/dev/fleet/test_fleet_tooling.py``
"""

from __future__ import annotations

import argparse
import io
import json
import logging
import os
import sys
import tempfile
import threading
import unittest
import urllib.error
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

os.environ.setdefault("DATA_PLANE_CONTROL_SECRET", "test-secret")

import _api
import action_storm
import fleet_launch
import read_bench


def setUpModule() -> None:
    """These tools log progress at WARNING/ERROR; the assertions are the output."""
    logging.disable(logging.CRITICAL)


def tearDownModule() -> None:
    logging.disable(logging.NOTSET)


class TransportFailureTests(unittest.TestCase):
    """A dropped socket must become a result, not an aborted sweep."""

    def test_url_error_returns_a_transport_failure_tuple(self) -> None:
        with mock.patch(
            "urllib.request.urlopen", side_effect=urllib.error.URLError("Connection refused")
        ):
            status, payload, latency = _api.request("GET", "/api/anything")

        self.assertEqual(status, _api.TRANSPORT_FAILURE_STATUS)
        self.assertIn("transport failure", payload["detail"])
        self.assertGreaterEqual(latency, 0.0)

    def test_timeout_returns_a_transport_failure_tuple(self) -> None:
        with mock.patch("urllib.request.urlopen", side_effect=TimeoutError("timed out")):
            status, payload, _latency = _api.request("GET", "/api/anything")

        self.assertEqual(status, _api.TRANSPORT_FAILURE_STATUS)
        self.assertIn("transport failure", payload["detail"])


class FleetLaunchSweepTests(unittest.TestCase):
    """One failed deployment must not end the sweep or fake convergence."""

    def _manifest(self, tmp: Path, entries: list[dict]) -> str:
        path = tmp / "manifest.json"
        path.write_text(json.dumps(entries), encoding="utf-8")
        return str(path)

    def test_sweep_continues_past_a_transport_failure(self) -> None:
        entries = [
            {"sid": "bot-a", "strategy_key": "k", "symbol": "SPY"},
            {"sid": "bot-b", "strategy_key": "k", "symbol": "QQQ"},
        ]
        attempted: list[str] = []

        def fake_deploy(sid, strategy_key, symbol, **_kwargs):
            attempted.append(sid)
            if sid == "bot-a":
                return _api.TRANSPORT_FAILURE_STATUS, {"detail": "transport failure: refused"}, 0.0
            return 201, {}, 0.1

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            manifest = self._manifest(tmp, entries)
            results = str(tmp / "results.jsonl")
            with (
                mock.patch.object(fleet_launch._api, "list_roster", return_value=[]),
                mock.patch.object(fleet_launch._api, "deploy", side_effect=fake_deploy),
                mock.patch.object(fleet_launch.time, "sleep"),
            ):
                exit_code = fleet_launch.launch(manifest, results, 0.0, 1)

            recorded = [json.loads(line) for line in Path(results).read_text().splitlines()]

        self.assertEqual(attempted, ["bot-a", "bot-b"])
        self.assertEqual([r["sid"] for r in recorded], ["bot-a", "bot-b"])
        self.assertEqual(exit_code, 1)

    def test_matching_roster_identity_is_skipped(self) -> None:
        existing = {"bot-a": ("k", "SPY")}
        entries = [{"sid": "bot-a", "strategy_key": "k", "symbol": "SPY"}]

        self.assertEqual(fleet_launch._pending_entries(entries, existing), [])

    def test_conflicting_roster_identity_refuses_convergence(self) -> None:
        existing = {"bot-a": ("other-strategy", "SPY")}
        entries = [{"sid": "bot-a", "strategy_key": "k", "symbol": "SPY"}]

        with self.assertRaises(fleet_launch.ManifestConflictError):
            fleet_launch._pending_entries(entries, existing)

    def test_override_reason_names_the_selected_account(self) -> None:
        with mock.patch.object(_api, "ACCOUNT_ID", "PA_OTHER_ACCOUNT"):
            reason = fleet_launch.override_reason()

        self.assertIn("PA_OTHER_ACCOUNT", reason)


class ActionStormWorkloadTests(unittest.TestCase):
    """A probe that cannot exercise its claim must refuse to run or pass."""

    def _parser(self) -> argparse.ArgumentParser:
        parser = argparse.ArgumentParser()
        parser.add_argument("mode", choices=["replay", "conflict", "fanout"])
        parser.add_argument("args", nargs="*")
        parser.add_argument("--n", type=int, default=4)
        parser.add_argument("--sids", default="")
        parser.add_argument("--reason", default="probe")
        return parser

    def _validate(self, argv: list[str]) -> None:
        parser = self._parser()
        with redirect_stderr(io.StringIO()):
            action_storm._validated_args(parser, parser.parse_args(argv))

    def test_replay_requires_both_positionals(self) -> None:
        with self.assertRaises(SystemExit):
            self._validate(["replay", "only-one"])

    def test_replay_requires_a_positive_sample(self) -> None:
        with self.assertRaises(SystemExit):
            self._validate(["replay", "sid", "action", "--n", "0"])

    def test_fanout_requires_at_least_one_sid(self) -> None:
        with self.assertRaises(SystemExit):
            self._validate(["fanout", "action"])

    def test_valid_workload_is_accepted(self) -> None:
        self._validate(["replay", "sid", "action", "--n", "2"])
        self._validate(["conflict", "sid", "a", "b"])
        self._validate(["fanout", "action", "--sids", "sid"])

    def test_worker_records_a_non_runtime_failure(self) -> None:
        results: list[dict] = []
        with mock.patch.object(_api, "post_action", side_effect=OSError("socket died")):
            action_storm._collect(results, threading.Lock(), "sid", "act", "idem-key", None, {})

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["status"], _api.TRANSPORT_FAILURE_STATUS)
        self.assertIn("socket died", results[0]["detail"])


class ReadBenchPurityTests(unittest.TestCase):
    """Purity is a measurement, not a default."""

    def test_unsampled_revision_is_not_reported_as_pure(self) -> None:
        def fake_request(_method, path, *_args, **_kwargs):
            # Catalog answers; the panel read fails, so no revision is sampled.
            if path.endswith("/catalog"):
                return 200, [], 0.01
            return 503, {"detail": "panel unavailable"}, 0.01

        with tempfile.TemporaryDirectory() as tmpdir:
            results = str(Path(tmpdir) / "read_bench.jsonl")
            with (
                mock.patch.object(read_bench._api, "request", side_effect=fake_request),
                redirect_stdout(io.StringIO()),
            ):
                exit_code = read_bench.run(1, 1, ["bot-a"], results)
            recorded = json.loads(Path(results).read_text().splitlines()[-1])

        self.assertEqual(recorded["revision_samples"], 0)
        self.assertIn("NOT SAMPLED", recorded["revision_drift"])
        self.assertEqual(exit_code, 1)

    def test_sampled_stable_revision_reports_purity(self) -> None:
        def fake_request(_method, path, *_args, **_kwargs):
            if path.endswith("/catalog"):
                return 200, [], 0.01
            return 200, {"revision": 7}, 0.01

        with tempfile.TemporaryDirectory() as tmpdir:
            results = str(Path(tmpdir) / "read_bench.jsonl")
            with (
                mock.patch.object(read_bench._api, "request", side_effect=fake_request),
                redirect_stdout(io.StringIO()),
            ):
                exit_code = read_bench.run(1, 2, ["bot-a"], results)
            recorded = json.loads(Path(results).read_text().splitlines()[-1])

        self.assertEqual(recorded["revision_samples"], 2)
        self.assertEqual(recorded["revision_drift"], "NONE (pure reads hold)")
        self.assertEqual(exit_code, 0)


if __name__ == "__main__":
    unittest.main()
