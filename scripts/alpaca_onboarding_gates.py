#!/usr/bin/env python3
"""Host-side gates for ``docs/runbooks/add-an-alpaca-account.md``.

Every gate here is a *whole answer*: the process exits ``0`` only when the gate
holds, and exits non-zero with a single ``DISQUALIFIED:`` / ``REFUSE:`` line on
stderr otherwise. Nothing asks the operator to read output and decide, and no
capture failure can be discarded by the next command in a pasted block — the
three rounds of hand-rolled runbook shell that this file replaces each shipped
a gate that could not refuse.

Stdlib only and 3.9-compatible, mirroring ``scripts/check_adr_status.py``: the
operator running this holds broker credentials on a real-money path and should
not have to install anything, and the macOS system ``python3`` is still 3.9.
``jq``/``shasum`` are likewise not portable prerequisites (GNU/Linux hosts ship
``sha256sum``, not ``shasum``), so no gate here shells out to either.

Subcommands
-----------
``credential-match``
    The lane container's live Alpaca credential pair must hash-match the env
    file the operator just wrote. Refuses on *either* side being empty, which
    is the case a naive comparison silently passes.

``capture-evidence``
    Captures ``/v2/account``, ``/v2/orders?status=open`` and ``/v2/positions``
    from Alpaca, gates the account as flat and quiet, retains all three raw
    responses inside the lane's artifacts volume, and writes the broker
    evidence file ``manage_alpaca_sqlite_clerk --broker-evidence`` reads.

``wal-checkpoint``
    Checkpoints a stale ``clerk.db-wal``/``-shm`` pair *without* creating a
    database: ``sqlite3.connect()`` on a typo'd path writes a new 0-byte
    ``clerk.db``, which then makes ``cutover-initialize`` refuse with
    ``AlreadyInitialized`` and bricks the lane.

``status``
    Read the fleet binding and deployment gates without starting a bot.
    ``--require roster`` checks routing; ``--require deploy --symbol SPY``
    also checks strategy permissions, account holds and that symbol's live data.

Run directly::

    python3 scripts/alpaca_onboarding_gates.py <subcommand> --help
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Final

# ── mode-derived constants ────────────────────────────────────────────────────
# Duplicated on purpose: this script runs on the host with no PythonDataService
# on ``sys.path``. Canonical implementation of the slot → credential-pair map is
# ``PythonDataService/app/broker/alpaca/profile/credentials.py`` (``_SLOT_FIELDS``
# plus the ``ALPACA_`` env prefix); ``test_alpaca_onboarding_gates.py`` pins this
# copy against that file so the two cannot drift.
MODES: Final[dict[str, dict[str, str]]] = {
    "paper": {
        "slot": "default",
        "base_url": "https://paper-api.alpaca.markets",
        "env_file": "deploy/fleet/env/paper.env",
        "key_variable": "ALPACA_API_KEY_ID",
        "secret_variable": "ALPACA_API_SECRET_KEY",
        "container": "alpaca-paper-clerk",
    },
    "live": {
        "slot": "live",
        "base_url": "https://api.alpaca.markets",
        "env_file": "deploy/fleet/env/live.env",
        "key_variable": "ALPACA_CREDENTIAL_LIVE_KEY_ID",
        "secret_variable": "ALPACA_CREDENTIAL_LIVE_SECRET_KEY",
        "container": "alpaca-live-clerk",
    },
}

DEFAULT_ARTIFACTS_ROOT: Final = "/app/artifacts/alpaca_clerk"
CONTAINER_PYTHON: Final = "/opt/venv/bin/python"
PHASE_PREFIX: Final[dict[str, str]] = {"initialize": "init", "plan": "plan"}
# Conservative: Alpaca account numbers are alphanumeric (``PA3ABCDEF``,
# ``318420190``). Also keeps operator input out of a generated Python literal
# and out of a ``podman`` argv as anything but a plain token.
ACCOUNT_ID_PATTERN: Final = re.compile(r"\A[A-Za-z0-9._-]{1,64}\Z")
HTTP_TIMEOUT_S: Final = 30.0
# sha256 of the empty string. Both sides of an absent credential hash to this,
# which is why a bare digest comparison passes the one case it exists to catch.
EMPTY_SHA256: Final = hashlib.sha256(b"").hexdigest()


class GateFailed(Exception):
    """A gate refused. The message is the whole operator-facing answer."""


# ── pure helpers (the gates themselves) ───────────────────────────────────────
def parse_env_file(text: str) -> dict[str, str]:
    """Parse an env file the way Compose's ``env_file:`` does.

    ``cut -d= -f2-`` — what the runbook used to do — strips neither surrounding
    quotes nor a trailing ``\\r`` and appends a newline of its own, so a quoted
    or CRLF value hashed differently on the host than in the container even when
    the recreate had taken correctly.
    """
    values: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.rstrip("\r").strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        values[key.strip()] = value
    return values


def sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def gate_credential_match(*, variable: str, host_digest: str, container_digest: str) -> None:
    """Both digests must be present, non-empty-valued, *and* equal.

    The non-empty guard is not decoration. Absent on both sides hashes to
    ``e3b0c442…`` on both sides and "matches", so a comparison without it hands
    a green check to exactly the operator this gate exists for: one who forgot
    the credential, or whose recreate picked up an empty env file.
    """
    if not host_digest or host_digest == EMPTY_SHA256:
        raise GateFailed(f"REFUSE: {variable} is absent or empty in the env file on the host")
    if not container_digest or container_digest == EMPTY_SHA256:
        raise GateFailed(f"REFUSE: {variable} is absent or empty inside the container")
    if host_digest != container_digest:
        raise GateFailed(
            f"REFUSE: {variable} inside the container does not match the env file — "
            "the Compose recreate did not take; do not bind this lane"
        )


def _require_zero_money(payload: dict[str, Any], field: str) -> None:
    try:
        raw = payload[field]
    except KeyError:
        raise GateFailed(
            f"DISQUALIFIED: the account capture has no {field!r} — it is not an Alpaca "
            "account payload (an error body reaches here looking like this)"
        ) from None
    if raw is None:
        raise GateFailed(f"DISQUALIFIED: the account capture reports {field} as null, not a number")
    try:
        value = float(raw)
    except (TypeError, ValueError):
        raise GateFailed(f"DISQUALIFIED: the account capture reports {field}={raw!r}, which is not a number") from None
    if value != 0.0:
        raise GateFailed(f"DISQUALIFIED: account is not flat — {field}={raw!r}; stop, do not cut over")


def gate_account_flat(payload: object) -> None:
    """``long_market_value`` and ``short_market_value`` must both read zero.

    Read by direct subscript, the way ``app/broker/alpaca/adapter.py:211-212``
    reads the same two fields: a missing key is an error body, not a zero.
    """
    if not isinstance(payload, dict):
        raise GateFailed(f"DISQUALIFIED: the account capture is a {type(payload).__name__}, not a JSON object")
    _require_zero_money(payload, "long_market_value")
    _require_zero_money(payload, "short_market_value")


def gate_account_identity(payload: dict[str, Any], *, account_id: str) -> None:
    try:
        captured = str(payload["account_number"])
    except KeyError:
        raise GateFailed("DISQUALIFIED: the account capture has no 'account_number' to identify it") from None
    if captured != account_id:
        raise GateFailed(
            f"DISQUALIFIED: the credentials answered for account {captured!r}, not the "
            f"{account_id!r} you are cutting over — wrong env file, or wrong lane"
        )


def gate_empty_array(payload: object, *, what: str) -> None:
    """Must be a JSON array of length zero.

    ``length == 0`` alone is not enough: ``jq 'length == 0'`` passes on ``{}``,
    and every realistic Alpaca error body is an object.
    """
    if not isinstance(payload, list):
        raise GateFailed(
            f"DISQUALIFIED: the {what} capture is a {type(payload).__name__}, not a JSON array — "
            "it is an error body, not evidence"
        )
    if payload:
        raise GateFailed(f"DISQUALIFIED: account has {len(payload)} {what}; stop, do not cut over")


def gate_checkpoint_row(row: object) -> None:
    """``PRAGMA wal_checkpoint(TRUNCATE)`` must report a real checkpoint.

    ``(0, -1, -1)`` is what an *empty* database returns — byte-identical to
    success under a "first element is 0" rule, and exactly what a typo'd path
    produces. ``(1, n, n)`` is the busy result: another connection holds the
    database; SQLite does not raise for it.
    """
    if not isinstance(row, list) or len(row) != 3 or not all(isinstance(item, int) for item in row):
        raise GateFailed(f"REFUSE: the checkpoint returned {row!r}, not a (busy, log, checkpointed) triple")
    busy, log_frames, checkpointed = row
    if busy != 0:
        raise GateFailed(
            f"REFUSE: the checkpoint reports busy={busy} ({row!r}) — something still holds the "
            "database open. Stop every governed bot for this account, and if the lane's own "
            "Clerk process is the holder, restart the lane container and re-run this gate. "
            "Never rm a -wal or -shm file."
        )
    if log_frames < 0 or checkpointed < 0:
        raise GateFailed(
            f"REFUSE: the checkpoint returned {row!r} — negative frame counts mean the database "
            "has no WAL at all, which on this path means the path is wrong, not that it worked"
        )


# ── side-effecting seams (patched in tests) ───────────────────────────────────
def _run(argv: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, capture_output=True, text=True, check=False)


def _fetch(url: str, headers: dict[str, str]) -> tuple[int, bytes]:
    """Return ``(status, body)``, including for an error response.

    Equivalent to ``curl --fail-with-body``: the body of a 401/403 is kept so
    the operator can read it, and the status is returned so the *caller* can
    abort on it. Unlike a pasted ``curl``, the caller here cannot forget to.
    """
    request = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_S) as response:
            return int(response.status), response.read()
    except urllib.error.HTTPError as exc:
        return int(exc.code), exc.read()
    except urllib.error.URLError as exc:
        raise GateFailed(f"DISQUALIFIED: could not reach Alpaca at {url}: {exc.reason}") from exc


def _podman(*argv: str) -> str:
    result = _run(["podman", *argv])
    if result.returncode != 0:
        raise GateFailed(f"REFUSE: `podman {' '.join(argv)}` failed ({result.returncode}): {result.stderr.strip()}")
    return result.stdout


# ── shared argument plumbing ──────────────────────────────────────────────────
def _resolve(args: argparse.Namespace, key: str) -> str:
    override = getattr(args, key, None)
    return str(override) if override else MODES[args.mode][key]


def _require_account_id(account_id: str) -> str:
    if not ACCOUNT_ID_PATTERN.fullmatch(account_id):
        raise GateFailed(f"REFUSE: {account_id!r} is not a plausible Alpaca account number")
    return account_id


def _read_credentials(args: argparse.Namespace) -> tuple[str, str, str]:
    """``(env_file_path, key_id, secret_key)`` for this mode's slot."""
    env_file = Path(_resolve(args, "env_file"))
    try:
        values = parse_env_file(env_file.read_text(encoding="utf-8"))
    except OSError as exc:
        raise GateFailed(f"REFUSE: cannot read {env_file}: {exc}") from exc
    mode = MODES[args.mode]
    return (
        env_file.as_posix(),
        values.get(mode["key_variable"], ""),
        values.get(mode["secret_variable"], ""),
    )


def _container_credential_hashes(container: str, variables: list[str]) -> dict[str, str]:
    """Hash the container's live values in-process; no value touches argv or a file."""
    snippet = (
        "import hashlib, json, os\n"
        f"names = {json.dumps(variables)}\n"
        "print(json.dumps({n: hashlib.sha256(os.environ.get(n, '').encode()).hexdigest()"
        " if os.environ.get(n) else '' for n in names}))\n"
    )
    raw = _podman("exec", container, CONTAINER_PYTHON, "-c", snippet)
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise GateFailed(f"REFUSE: the container did not return credential hashes: {raw.strip()!r}") from exc
    return {str(key): str(value) for key, value in parsed.items()}


# ── subcommand: credential-match ──────────────────────────────────────────────
def command_credential_match(args: argparse.Namespace) -> None:
    container = _resolve(args, "container")
    env_path, key_value, secret_value = _read_credentials(args)
    key_variable = MODES[args.mode]["key_variable"]
    secret_variable = MODES[args.mode]["secret_variable"]
    hashes = _container_credential_hashes(container, [key_variable, secret_variable])
    for variable, host_value in ((key_variable, key_value), (secret_variable, secret_value)):
        # Compared as digests throughout: the container prints only a hash, and
        # the host value never leaves this process.
        gate_credential_match(
            variable=variable,
            host_digest=sha256_hex(host_value) if host_value else "",
            container_digest=hashes.get(variable, ""),
        )
    print(f"OK: {container} holds the {args.mode} credential pair written to {env_path}")


# ── subcommand: capture-evidence ──────────────────────────────────────────────
def command_capture_evidence(args: argparse.Namespace) -> None:
    account_id = _require_account_id(args.account_id)
    container = _resolve(args, "container")
    base_url = _resolve(args, "base_url").rstrip("/")
    prefix = PHASE_PREFIX[args.phase]
    capture_dir = args.capture_dir or ("broker_captures/cutover-" + time.strftime("%Y-%m-%d", time.gmtime()))
    env_path, key_value, secret_value = _read_credentials(args)
    if not key_value or not secret_value:
        raise GateFailed(f"REFUSE: {env_path} does not hold a {args.mode} key/secret pair — nothing to authenticate with")

    out_dir = Path(args.out_dir or Path(tempfile.gettempdir()) / f"alpaca-onboarding-{args.mode}-{prefix}")
    out_dir.mkdir(parents=True, exist_ok=True)
    out_dir.chmod(0o700)

    headers = {"APCA-API-KEY-ID": key_value, "APCA-API-SECRET-KEY": secret_value}
    # Taken before the first request, so the evidence is never claimed fresher
    # than it is; `--max-evidence-age-ms` is measured against this.
    observed_at_ms = int(time.time() * 1000)
    payloads: dict[str, Any] = {}
    for name, path in (("account", "/v2/account"), ("orders", "/v2/orders?status=open"), ("positions", "/v2/positions")):
        target = out_dir / f"{prefix}-{name}.json"
        status, body = _fetch(f"{base_url}{path}", headers)
        target.write_bytes(body)
        target.chmod(0o600)
        if not 200 <= status < 300:
            raise GateFailed(
                f"DISQUALIFIED: the {name} capture returned HTTP {status}. Its body is at {target} "
                "for diagnosis; it is an error response, not evidence"
            )
        try:
            payloads[name] = json.loads(body)
        except json.JSONDecodeError as exc:
            raise GateFailed(f"DISQUALIFIED: the {name} capture at {target} is not JSON: {exc}") from exc

    gate_account_flat(payloads["account"])
    gate_account_identity(payloads["account"], account_id=account_id)
    gate_empty_array(payloads["orders"], what="open orders")
    gate_empty_array(payloads["positions"], what="positions")

    proof_reference = f"{capture_dir}/{prefix}-positions.json"
    evidence = {
        "account_id": account_id,
        "account_mode": args.mode,
        "observed_at_ms": observed_at_ms,
        "proof_reference": proof_reference,
        # Transcribed from the three gates above, which have just proven both.
        "positions": {},
        "open_order_ids": [],
    }
    evidence_path = out_dir / f"{prefix}-evidence.json"
    evidence_path.write_text(json.dumps(evidence, indent=2) + "\n", encoding="utf-8")
    evidence_path.chmod(0o600)

    artifacts_root = args.artifacts_root.rstrip("/")
    container_dir = f"{artifacts_root}/{capture_dir}"
    _podman("exec", container, "mkdir", "-p", container_dir)
    for name in ("account", "orders", "positions", "evidence"):
        local = out_dir / f"{prefix}-{name}.json"
        _podman("cp", str(local), f"{container}:{container_dir}/{local.name}")

    print(f"OK: account {account_id} is flat, has no open orders, and all three captures are retained.")
    print(f"  proof_reference : {proof_reference}")
    print(f"  observed_at_ms  : {observed_at_ms}")
    print(f"  --broker-evidence {container_dir}/{evidence_path.name}")


# ── subcommand: wal-checkpoint ────────────────────────────────────────────────
_CHECKPOINT_SNIPPET = """\
import json, os, sqlite3, sys
db = {db!r}
if not os.path.isfile(db):
    print(json.dumps({{"missing": db}}))
    sys.exit(0)
conn = sqlite3.connect("file:" + db + "?mode=rw", uri=True)
try:
    row = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
finally:
    conn.close()
print(json.dumps({{"row": list(row)}}))
"""


def command_wal_checkpoint(args: argparse.Namespace) -> None:
    account_id = _require_account_id(args.account_id)
    container = _resolve(args, "container")
    db_path = f"{args.artifacts_root.rstrip('/')}/accounts/alpaca/{account_id}/clerk.db"
    # ``mode=rw`` plus the isfile guard: a plain ``sqlite3.connect()`` on a
    # typo'd path CREATES a 0-byte clerk.db in the custody volume, after which
    # ``ClerkSqliteRepository.initialize`` refuses with ``AlreadyInitialized``
    # and the only obvious recovery is the ``rm`` this whole procedure forbids.
    raw = _podman("exec", container, CONTAINER_PYTHON, "-c", _CHECKPOINT_SNIPPET.format(db=db_path))
    try:
        result = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise GateFailed(f"REFUSE: the checkpoint did not report a result: {raw.strip()!r}") from exc
    if "missing" in result:
        raise GateFailed(
            f"REFUSE: {result['missing']} does not exist in {container}. Nothing was created. "
            "Check --account-id and --artifacts-root before re-running"
        )
    gate_checkpoint_row(result.get("row"))
    print(f"OK: checkpointed {db_path} in {container}; result {tuple(result['row'])}")


# ── entry point ───────────────────────────────────────────────────────────────
def gate_lane_ready(
    lane: dict[str, Any], *, mode: str, require: str, deploy: dict[str, Any] | None,
    roster: dict[str, Any] | list[Any] | None = None,
) -> None:
    """Process health is insufficient: require confirmed custody, then admission."""
    summary = lane.get("provider_summary") or {}
    if lane.get("lifecycle_state") != "ready" or not summary.get("confirmed_by_current_session"):
        raise GateFailed(
            f"REFUSE: lane is {lane.get('lifecycle_state', 'unknown')}; its current worker "
            "has not confirmed a ready account binding. Read the startup refusal above."
        )
    if summary.get("authority_state") not in {"real_paper", "shadow", "real_live"}:
        raise GateFailed(
            "REFUSE: lane has not reported a usable custody authority. "
            "After startup, wait for its next heartbeat; otherwise read the startup refusal."
        )
    expected = {"real_paper"} if mode == "paper" else {"shadow", "real_live"}
    if summary["authority_state"] not in expected:
        raise GateFailed(
            f"REFUSE: authority {summary['authority_state']} does not match --mode {mode}; "
            f"expected {' or '.join(sorted(expected))}. Check the lane container and active profile."
        )
    if isinstance(roster, dict) and "http_status" in roster:
        raise GateFailed(f"REFUSE: roster read failed with HTTP {roster['http_status']}: {roster.get('refusal')}")
    if require == "deploy" and (deploy is None or not deploy.get("eligibility", {}).get("eligible")):
        reasons = [] if deploy is None else [
            check["evidence_summary"] for check in deploy.get("readiness_checks", []) if not check["ready"]
        ]
        raise GateFailed("REFUSE: deployment blocked: " + "; ".join(reasons or ["deployment check unavailable"]))


_STATUS_READ_SNIPPET = """\
import json, os, sys, urllib.request, urllib.error, urllib.parse
clerk_id = sys.argv[1]
symbol = sys.argv[2]
headers = {"X-Data-Plane-Control-Secret": os.environ["DATA_PLANE_CONTROL_SECRET"]}
def get(path):
    request = urllib.request.Request("http://127.0.0.1:8000" + path, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        return {"http_status": error.code, "refusal": json.load(error)}
directory = get("/api/broker-clerks")
lane = next((item for item in directory.get("clerks", []) if item["clerk_id"] == clerk_id), None)
if lane is None:
    raise SystemExit("The lane is missing from the fleet directory; inspect coordinator health.")
prefix = "/api/brokers/alpaca/clerks/" + clerk_id
selection = get(prefix + "/configuration/selection")
account = selection.get("effective_account_id")
query = "?" + urllib.parse.urlencode({"symbol": symbol}) if symbol else ""
deploy = get(prefix + "/accounts/" + account + "/bots/deploy" + query) if account else None
roster = get(prefix + "/accounts/" + account + "/bots/catalog") if account else None
sys.stdout.write(json.dumps({"lane": lane, "selection": selection, "deploy": deploy, "roster": roster}))
"""


_STARTUP_READ_SNIPPET = """\
import json, os, sys, urllib.request, urllib.error
headers = {
    "X-Fleet-Coordinator-Token": os.environ["FLEET_COORDINATOR_SERVICE_TOKEN"],
    "X-Fleet-Clerk-Id": os.environ["FLEET_CLERK_ID"], "X-Fleet-Broker": "alpaca",
}
request = urllib.request.Request("http://127.0.0.1:8000/api/brokers/alpaca/clerk/status", headers=headers)
try:
    with urllib.request.urlopen(request, timeout=15) as response:
        json.load(response)
        sys.stdout.write("Clerk custody is installed.\\n")
except urllib.error.HTTPError as error:
    sys.stdout.write(json.dumps(json.load(error)) + "\\n")
except urllib.error.URLError:
    sys.stdout.write("The worker has not opened its API yet. Wait for startup, then rerun status.\\n")
"""


def command_status(args: argparse.Namespace) -> None:
    symbol = (args.symbol or "").strip().upper()
    if args.require == "deploy" and not symbol:
        raise GateFailed("REFUSE: --require deploy needs --symbol (for example --symbol SPY).")
    container = _resolve(args, "container")
    clerk_id = _podman(
        "exec", container, CONTAINER_PYTHON, "-c",
        "import os,sys; sys.stdout.write(os.environ['FLEET_CLERK_ID'])",
    ).strip()
    data = json.loads(_podman(
        "exec", args.coordinator, CONTAINER_PYTHON, "-c", _STATUS_READ_SNIPPET, clerk_id, symbol,
    ))
    lane, selection, deploy = data["lane"], data["selection"], data["deploy"]
    summary = lane.get("provider_summary") or {}
    sys.stdout.write(json.dumps({
        "container": container, "clerk_id": clerk_id,
        "state": lane["lifecycle_state"], "authority": summary.get("authority_state"),
        "account": selection.get("effective_account_id"),
        "symbol": symbol or None,
        "last_apply_refusal": selection.get("last_apply_refusal_reason"),
        "roster_url": (
            f"http://localhost:4200/brokers/alpaca/clerks/{clerk_id}/accounts/"
            f"{selection['effective_account_id']}/bots"
            if selection.get("effective_account_id") else None
        ),
        "deployment_blockers": [] if deploy is None else [
            check["evidence_summary"] for check in deploy.get("readiness_checks", []) if not check["ready"]
        ],
    }, indent=2) + "\n")
    if lane["lifecycle_state"] != "ready" or summary.get("authority_state") not in {"real_paper", "shadow", "real_live"}:
        sys.stdout.write(_podman("exec", container, CONTAINER_PYTHON, "-c", _STARTUP_READ_SNIPPET))
    if lane["lifecycle_state"] == "ready" and not selection.get("effective_account_id"):
        raise GateFailed("REFUSE: ready lane's effective account could not be read; inspect configuration access")
    gate_lane_ready(lane, mode=args.mode, require=args.require, deploy=deploy, roster=data["roster"])
    scope = f" / {symbol}" if symbol else ""
    sys.stdout.write(f"OK: {args.require} readiness verified for {container}{scope}. No bot was started.\n")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="alpaca_onboarding_gates.py", description=__doc__.splitlines()[0])
    subparsers = parser.add_subparsers(dest="command", required=True)

    def add_common(sub: argparse.ArgumentParser) -> None:
        sub.add_argument("--mode", choices=sorted(MODES), required=True)
        sub.add_argument("--container", help="Override the lane container name for this mode.")

    match = subparsers.add_parser("credential-match", help="The lane's live credentials must hash-match the env file.")
    add_common(match)
    match.add_argument("--env-file", help="Override the env file for this mode.")
    match.set_defaults(handler=command_credential_match)

    capture = subparsers.add_parser("capture-evidence", help="Capture, gate and retain the three broker captures.")
    add_common(capture)
    capture.add_argument("--phase", choices=sorted(PHASE_PREFIX), required=True)
    capture.add_argument("--account-id", required=True)
    capture.add_argument("--env-file", help="Override the env file for this mode.")
    capture.add_argument("--base-url", help="Override the Alpaca base URL for this mode.")
    capture.add_argument("--capture-dir", help="Path under --artifacts-root. Default: broker_captures/cutover-<today>.")
    capture.add_argument("--out-dir", help="Local directory for the captures. Created 0700.")
    capture.add_argument("--artifacts-root", default=DEFAULT_ARTIFACTS_ROOT)
    capture.set_defaults(handler=command_capture_evidence)

    checkpoint = subparsers.add_parser("wal-checkpoint", help="Checkpoint a stale WAL without creating a database.")
    add_common(checkpoint)
    checkpoint.add_argument("--account-id", required=True)
    checkpoint.add_argument("--artifacts-root", default=DEFAULT_ARTIFACTS_ROOT)
    checkpoint.set_defaults(handler=command_wal_checkpoint)

    status = subparsers.add_parser("status", help="Check the account binding and launch blockers, read-only.")
    add_common(status)
    status.add_argument("--coordinator", default="polygon-data-service")
    status.add_argument("--require", choices=["roster", "deploy"], default="roster")
    status.add_argument("--symbol", help="Symbol to check; required with --require deploy (for example SPY).")
    status.set_defaults(handler=command_status)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        args.handler(args)
    except GateFailed as failure:
        print(str(failure), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
