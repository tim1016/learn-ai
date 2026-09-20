"""Contract: the lane lake-catalog role is least-privilege by construction.

#2166: the clerk lanes read lake evidence in their own process (#2163) but
must never hold the superuser URL compose.yaml hands the combined role — a
clerk also carries Alpaca execution credentials, so its database login is
the blast radius of a lane compromise. The provisioning SQL
(``deploy/fleet/sql/provision-fleet-lake-catalog-role.sql``) is therefore
pinned here: read-only, over exactly the enumerated tables a lane-served
read touches, idempotent, with no write/DDL path and no reach into the
account/order domain the .NET backend owns.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SQL_PATH = ROOT / "deploy" / "fleet" / "sql" / "provision-fleet-lake-catalog-role.sql"

LANE_TABLES = frozenset(
    {
        "research_schema_migrations",
        "research_validation_golden_runs",
        "research_golden_validation_reviews",
        "research_parity_verdicts",
        "research_backtest_runs",
    }
)

# The account/order domain the .NET backend owns, plus the lake/research
# tables no lane-served read resolves (the artifact catalog, backtest
# trades, and the Recency family — all coordinator-served surfaces). A lane
# read never touches the former and must not reach the latter: their names
# in any grant are the exact widening #2166 exists to remove.
NEVER_GRANTED_TABLES = frozenset(
    {
        "Accounts",
        "Orders",
        "Positions",
        "PositionLots",
        "PortfolioSnapshots",
        "PortfolioTrades",
        "RiskRules",
        "ReferenceData",
        "Tickers",
        "clerk_transactions",
        "clerk_transaction_events",
        "clerk_transaction_feed_status",
        "clerk_transaction_projection_cursors",
        "DataLakeArtifacts",
        "research_backtest_run_trades",
        "RecencyLaunches",
        "RecencyRuns",
        "RecencyTrades",
        "RecencyTradeMemberships",
    }
)


def _code() -> str:
    """The SQL with comments stripped, so pins target executable statements."""
    lines = [
        line
        for line in SQL_PATH.read_text(encoding="utf-8").splitlines()
        if not line.strip().startswith("--")
    ]
    return "\n".join(lines)


def test_role_creation_is_idempotent() -> None:
    """Re-running provisioning (e.g. after migrations add tables) must be a
    no-op for the role itself: CREATE ROLE has no IF NOT EXISTS, so an
    unguarded script fails on its second run and an operator's safest move
    becomes not re-running it."""
    code = _code()
    assert "CREATE ROLE fleet_lake_catalog" in code
    assert (
        re.search(
            r"IF NOT EXISTS \(\s*SELECT 1 FROM pg_roles WHERE rolname = 'fleet_lake_catalog'\s*\)",
            code,
        )
        is not None
    )


def test_role_holds_no_privileged_flags_and_no_literal_password() -> None:
    """The role is a plain login: every administrative flag is explicitly
    refused (not merely omitted by CREATE ROLE defaults), and the password
    arrives as a psql variable — a generated secret never belongs in a
    committed file."""
    code = _code()
    for flag in (
        "NOSUPERUSER",
        "NOCREATEDB",
        "NOCREATEROLE",
        "NOREPLICATION",
        "NOBYPASSRLS",
    ):
        assert flag in code, f"missing explicit {flag}"
    assert re.search(r"(?<!NO)SUPERUSER", code) is None
    assert "PASSWORD :'lane_password'" in code
    assert re.search(r"PASSWORD\s+'[^:]", code) is None


def test_every_grant_is_read_only() -> None:
    """No grant may carry a write, DDL, or blanket privilege — not ALL, not
    ALL TABLES, not default privileges (the runbook's re-run step is the
    documented way new tables become readable, deliberately)."""
    grants = re.findall(r"GRANT\s+(.*?);", _code(), flags=re.DOTALL)
    assert grants, "no GRANT statements found"
    for grant in grants:
        forbidden = (
            "INSERT",
            "UPDATE",
            "DELETE",
            "TRUNCATE",
            "CREATE",
            "EXECUTE",
            "REFERENCES",
            "ALL",
        )
        assert not any(word in grant.upper() for word in forbidden), (
            f"write or blanket privilege in grant: GRANT {grant}"
        )


def test_table_grants_name_exactly_the_lane_served_tables() -> None:
    """The SELECT surface is an enumerated, exact set — the tables the
    lane-served deploy path actually resolves (golden dossier runs,
    reviews, parity verdicts, and the backtest-run source those dossiers
    key into, plus the migration ledger ensure_schema() reads first).
    Nothing wider: the artifact catalog, backtest trades, and the Recency
    family are coordinator-served surfaces no lane read touches. Adding or
    removing a table is a deliberate edit to both sides of this pin, never
    a silent widening."""
    granted: set[str] = set()
    for match in re.finditer(
        r"GRANT\s+SELECT\s+ON\s+TABLE\s+(.+?)\s+TO\s+fleet_lake_catalog",
        _code(),
        flags=re.DOTALL,
    ):
        for table in match.group(1).split(","):
            granted.add(table.strip().removeprefix("public.").strip('"'))
    assert granted == set(LANE_TABLES), (
        f"lane SELECT surface drifted: {granted ^ set(LANE_TABLES)}"
    )


def test_no_grant_reaches_tables_outside_the_lane_surface() -> None:
    for table in NEVER_GRANTED_TABLES:
        assert table not in _code(), (
            f"{table} must never appear in the lane role's provisioning SQL"
        )


def test_reprovisioning_converges_instead_of_accumulating() -> None:
    """Re-running the script after the allowlist narrows (or after an
    operator's temporary widening elsewhere) must actually remove the
    stale privilege: every table privilege is revoked before the allowlist
    is applied, so the role's effective surface is always exactly the list
    above rather than the union of everything it was ever granted."""
    code = _code()
    revoke = code.find("REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA public FROM fleet_lake_catalog")
    grant = code.find("GRANT SELECT ON TABLE")
    assert revoke != -1, "no converging REVOKE of existing table privileges"
    assert grant != -1
    assert revoke < grant, "the REVOKE must precede the allowlist GRANTs"
    assert (
        "REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public FROM fleet_lake_catalog"
        in code
    )


def test_the_inherited_temporary_table_write_path_is_closed() -> None:
    """PostgreSQL grants TEMPORARY on every database to PUBLIC, and a role
    cannot selectively refuse an inherited PUBLIC privilege — so the
    script must close the wildcard at the PUBLIC boundary or the lane role
    can still create and fill temp tables despite its flags and narrow
    grants (a write path and a disk-exhaustion vector under the
    clerk-compromise scenario this role exists to contain)."""
    assert "REVOKE TEMPORARY ON DATABASE postgres FROM PUBLIC" in _code()
