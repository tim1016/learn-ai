"""Regression tests for scripts/alpaca_onboarding_gates.py.

Stdlib-only (unittest), mirroring the gates' own zero-dependency design — the
CI job that runs them installs nothing but Python itself.

Every reproduction the 2026-09-15 review recorded against the runbook's pasted
shell is pinned here as a test, so the gate that could not refuse it cannot come
back: the empty-credential false match, the Alpaca 401 body that passed the
flatness check, ``jq 'length == 0'`` passing on ``{}``, and the WAL checkpoint
that created a 0-byte database and then reported ``(0, -1, -1)`` as success.

Run directly: ``python3 scripts/test_alpaca_onboarding_gates.py``
"""

from __future__ import annotations

import contextlib
import io
import json
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import alpaca_onboarding_gates as gates

# Hashes reproduced on the host, 2026-09-15.
SHA_NO_NEWLINE = "2ae8306efc9bf116bd9a46b55a9003b474777ffbe56e3a20a66d96f1066eee96"
SHA_WITH_NEWLINE = "1734a793dab5c12434e1502d5ed5c4e902e31e8848a0823af279d6a8ec21cc3b"
SHA_EMPTY = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"

# A real Alpaca 401 body, verbatim from the review's reproduction.
ALPACA_401 = {"code": 40110000, "message": "request is not authorized"}
FLAT_ACCOUNT = {
    "account_number": "PA3TESTACCOUNT",
    "status": "ACTIVE",
    "long_market_value": "0",
    "short_market_value": "0",
}


class EnvFileParsing(unittest.TestCase):
    def test_plain_value(self) -> None:
        self.assertEqual(gates.parse_env_file("ALPACA_API_KEY_ID=PKTESTVALUE123\n"), {"ALPACA_API_KEY_ID": "PKTESTVALUE123"})

    def test_no_trailing_newline_is_hashed(self) -> None:
        """``cut -d= -f2- | shasum`` hashed a newline the container never has."""
        value = gates.parse_env_file("K=PKTESTVALUE123\n")["K"]
        self.assertEqual(gates.sha256_hex(value), SHA_NO_NEWLINE)
        self.assertNotEqual(gates.sha256_hex(value), SHA_WITH_NEWLINE)

    def test_quotes_are_stripped_like_compose(self) -> None:
        self.assertEqual(gates.parse_env_file('K="PKTESTVALUE123"\n')["K"], "PKTESTVALUE123")
        self.assertEqual(gates.parse_env_file("K='PKTESTVALUE123'\n")["K"], "PKTESTVALUE123")

    def test_carriage_return_is_stripped(self) -> None:
        self.assertEqual(gates.parse_env_file("K=PKTESTVALUE123\r\n")["K"], "PKTESTVALUE123")

    def test_comments_blanks_and_junk_are_skipped(self) -> None:
        parsed = gates.parse_env_file("# comment\n\nnot-an-assignment\nK=V\n")
        self.assertEqual(parsed, {"K": "V"})

    def test_value_may_contain_equals(self) -> None:
        self.assertEqual(gates.parse_env_file("K=a=b=c\n")["K"], "a=b=c")

    def test_absent_variable_is_absent_not_empty_string_match(self) -> None:
        self.assertNotIn("ALPACA_API_KEY_ID", gates.parse_env_file("OTHER=1\n"))


class CredentialMatchGate(unittest.TestCase):
    def test_equal_non_empty_digests_pass(self) -> None:
        digest = gates.sha256_hex("PKTESTVALUE123")
        gates.gate_credential_match(variable="K", host_digest=digest, container_digest=digest)

    def test_both_sides_absent_refuses(self) -> None:
        """The exact case the check was added for, which it used to pass."""
        with self.assertRaises(gates.GateFailed):
            gates.gate_credential_match(variable="K", host_digest="", container_digest="")

    def test_both_sides_hash_of_empty_string_refuses(self) -> None:
        self.assertEqual(gates.EMPTY_SHA256, SHA_EMPTY)
        with self.assertRaises(gates.GateFailed):
            gates.gate_credential_match(variable="K", host_digest=SHA_EMPTY, container_digest=SHA_EMPTY)

    def test_container_empty_refuses(self) -> None:
        with self.assertRaises(gates.GateFailed):
            gates.gate_credential_match(variable="K", host_digest=SHA_NO_NEWLINE, container_digest="")

    def test_mismatch_refuses(self) -> None:
        with self.assertRaises(gates.GateFailed):
            gates.gate_credential_match(variable="K", host_digest=SHA_NO_NEWLINE, container_digest=SHA_WITH_NEWLINE)


class AccountFlatnessGate(unittest.TestCase):
    def test_flat_account_passes(self) -> None:
        gates.gate_account_flat(FLAT_ACCOUNT)

    def test_renamed_or_absent_fields_refuse(self) -> None:
        with self.assertRaises(gates.GateFailed):
            gates.gate_account_flat({"status": "ACTIVE"})

    def test_alpaca_401_body_refuses(self) -> None:
        with self.assertRaises(gates.GateFailed):
            gates.gate_account_flat(ALPACA_401)

    def test_null_market_value_refuses(self) -> None:
        with self.assertRaises(gates.GateFailed):
            gates.gate_account_flat({**FLAT_ACCOUNT, "long_market_value": None})

    def test_non_zero_long_refuses(self) -> None:
        with self.assertRaises(gates.GateFailed):
            gates.gate_account_flat({**FLAT_ACCOUNT, "long_market_value": "1234.50"})

    def test_non_zero_short_refuses(self) -> None:
        with self.assertRaises(gates.GateFailed):
            gates.gate_account_flat({**FLAT_ACCOUNT, "short_market_value": "-500"})

    def test_array_payload_refuses(self) -> None:
        with self.assertRaises(gates.GateFailed):
            gates.gate_account_flat([])

    def test_identity_mismatch_refuses(self) -> None:
        gates.gate_account_identity(FLAT_ACCOUNT, account_id="PA3TESTACCOUNT")
        with self.assertRaises(gates.GateFailed):
            gates.gate_account_identity(FLAT_ACCOUNT, account_id="318420190")
        with self.assertRaises(gates.GateFailed):
            gates.gate_account_identity({"status": "ACTIVE"}, account_id="PA3TESTACCOUNT")


class EmptyArrayGate(unittest.TestCase):
    def test_empty_array_passes(self) -> None:
        gates.gate_empty_array([], what="positions")

    def test_empty_object_refuses(self) -> None:
        """``jq 'length == 0'`` passes on ``{}``; this must not."""
        with self.assertRaises(gates.GateFailed):
            gates.gate_empty_array({}, what="open orders")

    def test_alpaca_error_body_refuses(self) -> None:
        with self.assertRaises(gates.GateFailed):
            gates.gate_empty_array(ALPACA_401, what="positions")

    def test_non_empty_array_refuses(self) -> None:
        with self.assertRaises(gates.GateFailed):
            gates.gate_empty_array([{"symbol": "SPY", "qty": "3"}], what="positions")


class CheckpointResultGate(unittest.TestCase):
    def test_real_checkpoint_passes(self) -> None:
        gates.gate_checkpoint_row([0, 0, 0])
        gates.gate_checkpoint_row([0, 3, 3])

    def test_empty_database_result_refuses(self) -> None:
        """``(0, -1, -1)`` is what a just-created 0-byte database returns."""
        with self.assertRaises(gates.GateFailed):
            gates.gate_checkpoint_row([0, -1, -1])

    def test_busy_result_refuses(self) -> None:
        with self.assertRaises(gates.GateFailed):
            gates.gate_checkpoint_row([1, 3, 3])

    def test_unparseable_result_refuses(self) -> None:
        for row in (None, [0, 0], "0,0,0", [0, "a", 0]):
            with self.assertRaises(gates.GateFailed):
                gates.gate_checkpoint_row(row)


class SlotMapParity(unittest.TestCase):
    """This script's mode → variable map must equal the canonical slot map.

    Canonical implementation: ``PythonDataService/app/broker/alpaca/profile/
    credentials.py`` (``_SLOT_FIELDS`` plus the ``ALPACA_`` ``env_prefix``).
    """

    def test_variable_names_match_credentials_module(self) -> None:
        source = (
            Path(__file__).resolve().parent.parent
            / "PythonDataService/app/broker/alpaca/profile/credentials.py"
        ).read_text(encoding="utf-8")
        self.assertIn('env_prefix="ALPACA_"', source)
        found = {
            name.lower(): (key, secret)
            for name, key, secret in re.findall(r'CREDENTIAL_SLOT_(\w+): \("(\w+)", "(\w+)"\)', source)
        }
        self.assertTrue(found, "could not read _SLOT_FIELDS out of credentials.py")
        for mode, spec in gates.MODES.items():
            key_field, secret_field = found[spec["slot"]]
            self.assertEqual(spec["key_variable"], "ALPACA_" + key_field.upper(), mode)
            self.assertEqual(spec["secret_variable"], "ALPACA_" + secret_field.upper(), mode)


def _fake_podman(recorder: list[list[str]]):
    def run(argv: list[str]) -> subprocess.CompletedProcess[str]:
        recorder.append(argv)
        if argv[:2] == ["podman", "cp"]:
            shutil.copyfile(argv[2], argv[3].split(":", 1)[1])
        elif argv[3:5] == ["mkdir", "-p"]:
            Path(argv[5]).mkdir(parents=True, exist_ok=True)
        return subprocess.CompletedProcess(argv, 0, "", "")

    return run


class CaptureEvidenceExitCodes(unittest.TestCase):
    """The whole answer is the exit status — no output for a human to read."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.env_file = self.tmp / "paper.env"
        self.env_file.write_text("ALPACA_API_KEY_ID=PKTESTVALUE123\nALPACA_API_SECRET_KEY=secret\n")
        self.out_dir = self.tmp / "captures"
        self.container_dir = self.tmp / "volume"
        self.container_dir.mkdir()
        self.calls: list[list[str]] = []
        self._original_run = gates._run
        gates._run = _fake_podman(self.calls)
        self.addCleanup(setattr, gates, "_run", self._original_run)
        self._original_fetch = gates._fetch
        self.addCleanup(setattr, gates, "_fetch", self._original_fetch)

    def _serve(self, responses: dict[str, tuple[int, object]]) -> None:
        def fetch(url: str, headers: dict[str, str]) -> tuple[int, bytes]:
            for fragment, (status, payload) in responses.items():
                if fragment in url:
                    return status, json.dumps(payload).encode()
            raise AssertionError(f"unexpected url {url}")

        gates._fetch = fetch

    def _invoke(self) -> tuple[int, str]:
        stderr = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(stderr):
            code = gates.main(
                [
                    "capture-evidence",
                    "--mode",
                    "paper",
                    "--phase",
                    "initialize",
                    "--account-id",
                    "PA3TESTACCOUNT",
                    "--env-file",
                    str(self.env_file),
                    "--out-dir",
                    str(self.out_dir),
                    "--capture-dir",
                    "broker_captures/cutover-2026-09-15",
                    "--artifacts-root",
                    str(self.container_dir),
                    "--container",
                    "alpaca-paper-clerk",
                ]
            )
        return code, stderr.getvalue()

    def test_flat_account_exits_zero_and_writes_evidence(self) -> None:
        self._serve({"/v2/account": (200, FLAT_ACCOUNT), "/v2/orders": (200, []), "/v2/positions": (200, [])})
        code, stderr = self._invoke()
        self.assertEqual((code, stderr), (0, ""))
        evidence = json.loads((self.out_dir / "init-evidence.json").read_text())
        self.assertEqual(evidence["positions"], {})
        self.assertEqual(evidence["open_order_ids"], [])
        self.assertEqual(evidence["account_id"], "PA3TESTACCOUNT")
        self.assertEqual(evidence["account_mode"], "paper")
        self.assertEqual(evidence["proof_reference"], "broker_captures/cutover-2026-09-15/init-positions.json")
        self.assertIsInstance(evidence["observed_at_ms"], int)
        # All four files reached the lane volume, under the cited capture dir.
        retained = self.container_dir / "broker_captures" / "cutover-2026-09-15"
        self.assertEqual(
            sorted(p.name for p in retained.iterdir()),
            ["init-account.json", "init-evidence.json", "init-orders.json", "init-positions.json"],
        )

    def test_401_error_body_exits_one(self) -> None:
        self._serve({"/v2/account": (401, ALPACA_401), "/v2/orders": (200, []), "/v2/positions": (200, [])})
        code, stderr = self._invoke()
        self.assertEqual(code, 1)
        self.assertIn("HTTP 401", stderr)
        # The body is kept for diagnosis, exactly like `curl --fail-with-body`…
        self.assertEqual(json.loads((self.out_dir / "init-account.json").read_text()), ALPACA_401)
        # …and nothing was retained in the lane volume.
        self.assertEqual(list(self.container_dir.iterdir()), [])

    def test_open_position_exits_one(self) -> None:
        self._serve(
            {
                "/v2/account": (200, FLAT_ACCOUNT),
                "/v2/orders": (200, []),
                "/v2/positions": (200, [{"symbol": "SPY", "qty": "3"}]),
            }
        )
        code, stderr = self._invoke()
        self.assertEqual(code, 1)
        self.assertIn("DISQUALIFIED", stderr)

    def test_open_orders_object_exits_one(self) -> None:
        self._serve({"/v2/account": (200, FLAT_ACCOUNT), "/v2/orders": (200, {}), "/v2/positions": (200, [])})
        code, stderr = self._invoke()
        self.assertEqual(code, 1)
        self.assertIn("DISQUALIFIED", stderr)

    def test_wrong_account_exits_one(self) -> None:
        self._serve({"/v2/account": (200, FLAT_ACCOUNT), "/v2/orders": (200, []), "/v2/positions": (200, [])})
        stderr = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(stderr):
            code = gates.main(
                [
                    "capture-evidence", "--mode", "paper", "--phase", "plan",
                    "--account-id", "318420190",
                    "--env-file", str(self.env_file), "--out-dir", str(self.out_dir),
                    "--artifacts-root", str(self.container_dir), "--container", "alpaca-paper-clerk",
                ]
            )
        self.assertEqual(code, 1)
        self.assertIn("wrong env file, or wrong lane", stderr.getvalue())

    def test_empty_env_file_exits_one_without_calling_alpaca(self) -> None:
        self.env_file.write_text("ALPACA_API_KEY_ID=\nALPACA_API_SECRET_KEY=\n")

        def refuse(url: str, headers: dict[str, str]) -> tuple[int, bytes]:
            raise AssertionError("must not reach Alpaca with an empty credential")

        gates._fetch = refuse
        code, stderr = self._invoke()
        self.assertEqual(code, 1)
        self.assertIn("REFUSE", stderr)


class CredentialMatchExitCodes(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.env_file = self.tmp / "paper.env"
        self.addCleanup(setattr, gates, "_run", gates._run)

    def _container_holds(self, values: dict[str, str]) -> None:
        digests = {name: (gates.sha256_hex(value) if value else "") for name, value in values.items()}

        def run(argv: list[str]) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(argv, 0, json.dumps(digests), "")

        gates._run = run

    def _invoke(self) -> tuple[int, str]:
        stderr = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(stderr):
            code = gates.main(
                ["credential-match", "--mode", "paper", "--env-file", str(self.env_file), "--container", "lane"]
            )
        return code, stderr.getvalue()

    def test_matching_pair_exits_zero(self) -> None:
        self.env_file.write_text("ALPACA_API_KEY_ID=PKTESTVALUE123\nALPACA_API_SECRET_KEY=s3cret\n")
        self._container_holds({"ALPACA_API_KEY_ID": "PKTESTVALUE123", "ALPACA_API_SECRET_KEY": "s3cret"})
        self.assertEqual(self._invoke(), (0, ""))

    def test_quoted_crlf_value_still_matches(self) -> None:
        """The correct-recreate direction: quoting and CRLF used to refuse."""
        self.env_file.write_bytes(b'ALPACA_API_KEY_ID="PKTESTVALUE123"\r\nALPACA_API_SECRET_KEY=s3cret\r\n')
        self._container_holds({"ALPACA_API_KEY_ID": "PKTESTVALUE123", "ALPACA_API_SECRET_KEY": "s3cret"})
        self.assertEqual(self._invoke(), (0, ""))

    def test_absent_on_both_sides_exits_one(self) -> None:
        """The case the old check was added for, and silently passed."""
        self.env_file.write_text("# nothing here\n")
        self._container_holds({"ALPACA_API_KEY_ID": "", "ALPACA_API_SECRET_KEY": ""})
        code, stderr = self._invoke()
        self.assertEqual(code, 1)
        self.assertIn("absent or empty in the env file", stderr)

    def test_recreate_did_not_take_exits_one(self) -> None:
        self.env_file.write_text("ALPACA_API_KEY_ID=PKNEWVALUE\nALPACA_API_SECRET_KEY=s3cret\n")
        self._container_holds({"ALPACA_API_KEY_ID": "PKSTALEVALUE", "ALPACA_API_SECRET_KEY": "s3cret"})
        code, stderr = self._invoke()
        self.assertEqual(code, 1)
        self.assertIn("the Compose recreate did not take", stderr)

    def test_secret_mismatch_alone_exits_one(self) -> None:
        self.env_file.write_text("ALPACA_API_KEY_ID=PKTESTVALUE123\nALPACA_API_SECRET_KEY=new\n")
        self._container_holds({"ALPACA_API_KEY_ID": "PKTESTVALUE123", "ALPACA_API_SECRET_KEY": "stale"})
        code, stderr = self._invoke()
        self.assertEqual(code, 1)
        self.assertIn("ALPACA_API_SECRET_KEY", stderr)

    def test_missing_env_file_exits_one(self) -> None:
        self._container_holds({"ALPACA_API_KEY_ID": "PKTESTVALUE123", "ALPACA_API_SECRET_KEY": "s3cret"})
        code, stderr = self._invoke()
        self.assertEqual(code, 1)
        self.assertIn("cannot read", stderr)


class WalCheckpointAgainstRealSqlite(unittest.TestCase):
    """The generated snippet runs for real, against a real SQLite database."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.account_dir = self.tmp / "accounts" / "alpaca" / "PA3TESTACCOUNT"
        self.account_dir.mkdir(parents=True)
        self.db = self.account_dir / "clerk.db"
        original = gates._run
        self.addCleanup(setattr, gates, "_run", original)
        gates._run = self._exec_locally

    @staticmethod
    def _exec_locally(argv: list[str]) -> subprocess.CompletedProcess[str]:
        """Stand in for `podman exec <lane> /opt/venv/bin/python -c <snippet>`."""
        assert argv[:2] == ["podman", "exec"] and argv[3] == gates.CONTAINER_PYTHON, argv
        return subprocess.run([sys.executable, *argv[4:]], capture_output=True, text=True, check=False)

    def _invoke(self, account_id: str = "PA3TESTACCOUNT") -> tuple[int, str]:
        stderr = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(stderr):
            code = gates.main(
                [
                    "wal-checkpoint", "--mode", "paper", "--account-id", account_id,
                    "--artifacts-root", str(self.tmp), "--container", "alpaca-paper-clerk",
                ]
            )
        return code, stderr.getvalue()

    def _seed_wal_database(self) -> None:
        conn = sqlite3.connect(self.db)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("CREATE TABLE t (a INTEGER)")
        conn.execute("INSERT INTO t VALUES (1)")
        conn.commit()
        conn.close()

    def test_real_wal_checkpoint_exits_zero(self) -> None:
        self._seed_wal_database()
        self.assertEqual(self._invoke(), (0, ""))

    def test_missing_database_exits_one_and_creates_nothing(self) -> None:
        """A typo'd account id must not write a 0-byte clerk.db into custody."""
        code, stderr = self._invoke(account_id="PA3TYPOACCOUNT")
        self.assertEqual(code, 1)
        self.assertIn("does not exist", stderr)
        self.assertFalse((self.tmp / "accounts" / "alpaca" / "PA3TYPOACCOUNT" / "clerk.db").exists())
        self.assertFalse(self.db.exists())

    def test_busy_database_exits_one_and_names_the_remedy(self) -> None:
        self._seed_wal_database()
        holder = sqlite3.connect(self.db)
        try:
            holder.execute("BEGIN IMMEDIATE")
            holder.execute("INSERT INTO t VALUES (2)")
            code, stderr = self._invoke()
        finally:
            holder.rollback()
            holder.close()
        self.assertEqual(code, 1)
        self.assertIn("busy=1", stderr)
        self.assertIn("restart the lane container", stderr)

    def test_implausible_account_id_is_refused_before_any_podman_call(self) -> None:
        def explode(argv: list[str]) -> subprocess.CompletedProcess[str]:
            raise AssertionError("must not shell out for an implausible account id")

        gates._run = explode
        code, stderr = self._invoke(account_id="../../etc")
        self.assertEqual(code, 1)
        self.assertIn("not a plausible Alpaca account number", stderr)


class LaneReadinessGate(unittest.TestCase):
    def test_confirmed_lane_cannot_hide_a_failed_roster_read(self) -> None:
        with self.assertRaisesRegex(gates.GateFailed, "roster read failed with HTTP 503"):
            gates.gate_lane_ready({
                "lifecycle_state": "ready",
                "provider_summary": {"authority_state": "shadow", "confirmed_by_current_session": True},
            }, require="roster", deploy=None, roster={"http_status": 503, "refusal": "custody unavailable"})

    def test_healthy_process_with_unconfirmed_binding_is_not_roster_ready(self) -> None:
        with self.assertRaisesRegex(gates.GateFailed, "starting"):
            gates.gate_lane_ready({
                "lifecycle_state": "starting",
                "provider_summary": {"authority_state": "real_paper"},
            }, require="roster", deploy=None)

    def test_shadow_is_ready_for_the_roster_without_real_money_activation(self) -> None:
        gates.gate_lane_ready({
            "lifecycle_state": "ready",
            "provider_summary": {"authority_state": "shadow", "confirmed_by_current_session": True},
        }, require="roster", deploy=None)

    def test_roster_ready_does_not_prove_launch_ready(self) -> None:
        with self.assertRaisesRegex(gates.GateFailed, "market-data feed"):
            gates.gate_lane_ready({
                "lifecycle_state": "ready",
                "provider_summary": {"authority_state": "shadow", "confirmed_by_current_session": True},
            }, require="deploy", deploy={
                "eligibility": {"eligible": False},
                "readiness_checks": [{"ready": False, "evidence_summary": "Missing market-data feed"}],
            })


if __name__ == "__main__":
    unittest.main()
