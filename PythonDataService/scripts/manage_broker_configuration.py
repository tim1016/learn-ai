"""Import an environment-configured installation into a broker profile.

The one-time cutover tool for ADR 0060. Run it once on a deployment that still
describes its Alpaca configuration in ``PythonDataService/.env``; afterwards the
worker binds a saved profile and the retired lines can be deleted.

Two steps, because the six numbers being imported bound real money::

    python -m scripts.manage_broker_configuration plan \
        --plan-out /app/artifacts/broker-config-import.json

    python -m scripts.manage_broker_configuration apply \
        --plan-file /app/artifacts/broker-config-import.json \
        --confirmation-token <token printed by plan>

``plan`` writes nothing and creates nothing -- not even the profiles database.
``apply`` re-reads the environment and refuses if it changed since the plan was
written. The token is the plan's own content hash, so a plan file whose numbers
were edited no longer verifies.

Run it against the Clerk volume, in the data-plane image, exactly as the arming
and shadow CLIs are run (see ``docs/references/alpaca-credential-slots.md``
§ "Cutting over")::

    podman compose run --rm --no-deps python-service \
        python -m scripts.manage_broker_configuration plan \
        --plan-out /app/artifacts/broker-config-import.json

``/app/artifacts`` is host-bind-mounted; ``/tmp`` inside a ``--rm`` container is
not, and a plan written there disappears before it can be applied.

Exit codes: ``0`` the command answered; ``1`` it could not be run as asked;
``2`` the ceremony refused.

Deliberately absent: a way to record an Apply. Apply is the operator's explicit
act and it is pressed on the configuration page (ADR 0060 Decision 4). This tool
stages at most, and staging governs nothing.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import fields
from pathlib import Path
from typing import Any

from app.broker.alpaca.clerk.ceremony import (
    DEFAULT_CONFIRMATION_TTL_MS,
    MAX_CONFIRMATION_TTL_MS,
)
from app.broker.alpaca.clerk.sqlite.operational_files import (
    atomic_write_json,
)
from app.broker.alpaca.profile import CREDENTIAL_SLOT_DEFAULT
from app.broker_configuration import runtime as broker_configuration_runtime
from app.broker_configuration.envelope import ValidatedLiveEnvelope
from app.broker_configuration.errors import BrokerConfigurationError
from app.broker_configuration.legacy_environment import (
    LegacyEnvironmentReadFailure,
    LegacyEnvironmentValues,
    read_legacy_values,
)
from app.broker_configuration.legacy_import import (
    DEFAULT_IMPORT_DISPLAY_NAME,
    IMPORT_PLAN_SCHEMA_VERSION,
    ConfigurationImportRefused,
    ExistingConfiguration,
    ImportedProfileRef,
    ImportPlan,
    apply_import,
    plan_import,
)
from app.broker_configuration.store import profiles_database_path
from app.config import settings
from app.utils.timestamps import now_ms_utc
from scripts._operator_cli import jsonable

# Derived, never hand-listed: a field added to ``ImportPlan`` must reach the
# plan file, and a hand-written set silently drops it.
_PLAN_FIELDS = {field.name for field in fields(ImportPlan)}


def _write(payload: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(jsonable(payload), sort_keys=True) + "\n")


def _clerk_dir(args: argparse.Namespace) -> Path:
    return args.clerk_dir or broker_configuration_runtime.resolve_clerk_dir()


def _existing_configuration(clerk_dir: Path) -> ExistingConfiguration:
    """Read the profiles database, or report an installation that has none.

    Existence is checked before opening because ``ProfilesStore.open`` creates
    and migrates the database -- and a *preview* that brought the profiles
    database into being would not be the read-only step this ceremony promises.
    """
    if not profiles_database_path(clerk_dir).exists():
        return ExistingConfiguration.absent()
    service = broker_configuration_runtime.build_service(clerk_dir=clerk_dir)
    try:
        return ExistingConfiguration.read(service)
    finally:
        service.close()


def _legacy_values() -> LegacyEnvironmentValues:
    values = read_legacy_values()
    if isinstance(values, LegacyEnvironmentReadFailure):
        raise ConfigurationImportRefused(
            "the retired Alpaca settings cannot be read: " + values.describe()
        )
    return values


class ConfigurationOperatorRefusal(ValueError):
    """This command cannot be run as asked — a named input is absent or malformed.

    Distinct from ``ConfigurationImportRefused``, which is the *ceremony*
    refusing something it understood. A plan file that is not a plan is exit 1;
    a plan that no longer verifies is exit 2. Same split the arming CLI makes.
    """


def _read_plan(path: Path) -> ImportPlan:
    """Rebuild a plan from its file, refusing any shape this code cannot read.

    Exact key-set equality, as ``manage_alpaca_sqlite_clerk._read_plan`` does: an
    extra or missing key means the file is not the document whose hash the token
    verifies, and guessing at it would be answering a question about the wrong
    plan.
    """
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigurationOperatorRefusal(f"cannot read the plan file: {exc}") from exc
    if not isinstance(payload, dict) or set(payload) != _PLAN_FIELDS:
        raise ConfigurationOperatorRefusal(
            "this file is not an import plan this version can read "
            f"(schema version {IMPORT_PLAN_SCHEMA_VERSION})"
        )
    if payload["schema_version"] != IMPORT_PLAN_SCHEMA_VERSION:
        raise ConfigurationOperatorRefusal(
            f"import plan is schema version {payload['schema_version']}, "
            f"not {IMPORT_PLAN_SCHEMA_VERSION}"
        )
    envelope = payload["live_envelope"]
    already = payload["already_imported"]
    return ImportPlan(
        **{
            **payload,
            "live_envelope": (
                None if envelope is None else ValidatedLiveEnvelope.from_mapping(envelope)
            ),
            "already_imported": (
                None if already is None else ImportedProfileRef(**already)
            ),
            "source_variables": tuple(payload["source_variables"]),
            "warnings": tuple(payload["warnings"]),
            "notes": tuple(payload["notes"]),
        }
    )


def _plan(args: argparse.Namespace) -> int:
    clerk_dir = _clerk_dir(args)
    plan = plan_import(
        existing=_existing_configuration(clerk_dir),
        values=_legacy_values(),
        display_name=args.display_name,
        credential_slot=args.credential_slot,
        operator_identity=settings.PANEL_OPERATOR_IDENTITY,
        stage_selection=not args.no_stage,
        confirmation_ttl_ms=args.confirmation_ttl_ms,
        now_ms=now_ms_utc(),
    )
    atomic_write_json(args.plan_out, jsonable(plan))
    # Printed beside the plan, never written into it: these are what the operator
    # does next, not content the token attests to. Same split the arming CLI
    # makes for its before-after envelope diff.
    _write(
        {
            "plan": plan,
            "plan_file": args.plan_out,
            "next_steps": _next_steps(plan, args.plan_out),
        }
    )
    return 0


def _next_steps(plan: ImportPlan, plan_out: Path) -> list[str]:
    # Warnings lead. They are about what the import *costs* while it is
    # half-finished, which an operator needs before they decide to start, not
    # after they have read six numbers.
    steps = [
        *plan.warnings,
        "Review every value above, especially the six live envelope values.",
        (
            "Apply this plan: python -m scripts.manage_broker_configuration apply "
            f"--plan-file {plan_out} --confirmation-token {plan.confirmation_token}"
        ),
        "Open the broker configuration page, verify the account and approve it.",
        "Press Apply on the configuration page to make the revision effective.",
    ]
    if plan.source_variables:
        # The plan's own record of what it read, not a second environment lookup:
        # a re-read could disagree with the document the token attests to, and
        # then the deletion list would name variables this import did not import.
        steps.append(
            "Delete "
            + ", ".join(plan.source_variables)
            + " from PythonDataService/.env — the worker refuses to bind while a "
            "retired setting is still present."
        )
    steps.append("Restart the service so the worker binds the profile.")
    return steps


def _apply(args: argparse.Namespace) -> int:
    clerk_dir = _clerk_dir(args)
    service = broker_configuration_runtime.build_service(clerk_dir=clerk_dir)
    try:
        receipt = apply_import(
            plan=_read_plan(args.plan_file),
            confirmation_token=args.confirmation_token,
            service=service,
            values=_legacy_values(),
            now_ms=now_ms_utc(),
        )
    finally:
        service.close()
    _write({"receipt": receipt})
    return 0


def _confirmation_ttl_ms(raw: str) -> int:
    """The same bound the ceremony enforces, on the flag that carries it.

    Bounded here so an out-of-range window is a usage error (exit 1) rather than
    a ceremony refusal (exit 2) — the operator mistyped a flag, the ceremony
    never got as far as refusing anything.
    """
    value = int(raw)
    if not 1 <= value <= MAX_CONFIRMATION_TTL_MS:
        raise argparse.ArgumentTypeError(
            f"must be between 1 and {MAX_CONFIRMATION_TTL_MS} milliseconds, not {value}"
        )
    return value


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="scripts.manage_broker_configuration",
        description="One-time import of environment configuration into a broker profile.",
        # ``exit_on_error=False`` here *and* on every subparser: argparse's own
        # usage exit is 2, which this CLI spends on "the ceremony refused".
        exit_on_error=False,
    )
    parser.add_argument(
        "--clerk-dir",
        type=Path,
        default=None,
        help="Override the Clerk directory that locates the profiles database.",
    )
    subparsers = parser.add_subparsers(dest="operation", required=True)

    plan = subparsers.add_parser(
        "plan", help="Describe the import. Writes no state.", exit_on_error=False
    )
    plan.add_argument("--plan-out", type=Path, required=True)
    plan.add_argument("--display-name", default=DEFAULT_IMPORT_DISPLAY_NAME)
    plan.add_argument("--credential-slot", default=CREDENTIAL_SLOT_DEFAULT)
    plan.add_argument(
        "--no-stage",
        action="store_true",
        help="Do not stage the imported revision. Staging governs nothing either way.",
    )
    plan.add_argument(
        "--confirmation-ttl-ms", type=_confirmation_ttl_ms, default=DEFAULT_CONFIRMATION_TTL_MS
    )

    apply_parser = subparsers.add_parser(
        "apply", help="Write the planned revision.", exit_on_error=False
    )
    apply_parser.add_argument("--plan-file", type=Path, required=True)
    apply_parser.add_argument("--confirmation-token", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        args = _build_parser().parse_args(argv)
        if args.operation == "plan":
            return _plan(args)
        return _apply(args)
    except ConfigurationImportRefused as exc:
        _write({"error": str(exc)})
        return 2
    except BrokerConfigurationError as exc:
        _write({"error": f"{exc.reason}: {exc.message}"})
        return 2
    except (ConfigurationOperatorRefusal, argparse.ArgumentError, OSError) as exc:
        # "Could not be run as asked", not "the ceremony refused". Note the
        # ordering: ``ConfigurationOperatorRefusal`` is a ``ValueError`` and must
        # be caught before any broader handler.
        _write({"error": str(exc)})
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
