"""Tests for app.engine.live.run_ledger.

Identity hash is computed by app.research.runs.hashing.canonical_json
+ SHA-256, which already has its own correctness tests under
tests/research/runs/test_hashing.py. These tests cover the live-runtime
wiring on top: identity payload composition, file-hashing on disk,
deterministic round-trip, and FileNotFoundError on missing inputs.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.engine.live import run_ledger as run_ledger_module
from app.engine.live.run_ledger import (
    LiveRunLedger,
    LiveRunStartDefaults,
    build_ledger,
    compute_run_id,
    read_ledger,
    write_ledger,
)


def _make_inputs(tmp_path: Path) -> tuple[Path, Path]:
    spec = tmp_path / "spec.json"
    qc_copy = tmp_path / "qc_audit.py"
    spec.write_text('{"strategy": "spy_ema_crossover"}', encoding="utf-8")
    qc_copy.write_text("# QC audit copy of SpyEmaCrossoverAlgorithm\n", encoding="utf-8")
    return spec, qc_copy


def test_compute_run_id_is_deterministic_for_same_payload() -> None:
    payload = {
        "code_sha": "abc123",
        "strategy_spec_sha256": "deadbeef",
        "qc_audit_copy_sha256": "cafe",
        "qc_cloud_backtest_id": "bt-1",
        "account_id": "DU111",
        "start_date_ms": 1_700_000_000_000,
        "live_config": {"symbol": "SPY"},
    }
    a = compute_run_id(**payload)
    b = compute_run_id(**payload)
    assert a == b
    assert len(a) == 64


def test_compute_run_id_changes_when_any_field_changes() -> None:
    base = {
        "code_sha": "abc123",
        "strategy_spec_sha256": "deadbeef",
        "qc_audit_copy_sha256": "cafe",
        "qc_cloud_backtest_id": "bt-1",
        "account_id": "DU111",
        "start_date_ms": 1_700_000_000_000,
        "live_config": {"symbol": "SPY"},
    }
    base_id = compute_run_id(**base)

    diffs = [
        {**base, "code_sha": "def456"},
        {**base, "strategy_spec_sha256": "feedface"},
        {**base, "qc_audit_copy_sha256": "babe"},
        {**base, "qc_cloud_backtest_id": "bt-2"},
        {**base, "account_id": "DU222"},
        {**base, "start_date_ms": 1_700_000_000_001},
        {**base, "live_config": {"symbol": "QQQ"}},
    ]
    for diff in diffs:
        assert compute_run_id(**diff) != base_id, f"identity collision on {diff}"


def test_build_ledger_hashes_inputs_and_assigns_run_id(tmp_path: Path) -> None:
    spec, qc_copy = _make_inputs(tmp_path)

    ledger = build_ledger(
        code_sha="abc123",
        strategy_spec_path=spec,
        qc_audit_copy_path=qc_copy,
        qc_cloud_backtest_id="bt-1",
        account_id="DU111",
        start_date_ms=1_700_000_000_000,
        live_config={"symbol": "SPY"},
    )
    assert isinstance(ledger, LiveRunLedger)
    assert len(ledger.run_id) == 64
    assert ledger.code_sha == "abc123"
    assert ledger.account_id == "DU111"
    assert len(ledger.strategy_spec_sha256) == 64
    assert len(ledger.qc_audit_copy_sha256) == 64


def test_build_ledger_raises_on_missing_strategy_spec(tmp_path: Path) -> None:
    _, qc_copy = _make_inputs(tmp_path)
    with pytest.raises(FileNotFoundError) as exc:
        build_ledger(
            code_sha="abc123",
            strategy_spec_path=tmp_path / "missing.json",
            qc_audit_copy_path=qc_copy,
            qc_cloud_backtest_id="bt-1",
            account_id="DU111",
            start_date_ms=1_700_000_000_000,
            live_config={},
        )
    assert "strategy_spec_path" in str(exc.value)


def test_build_ledger_raises_on_missing_qc_audit_copy(tmp_path: Path) -> None:
    spec, _ = _make_inputs(tmp_path)
    with pytest.raises(FileNotFoundError) as exc:
        build_ledger(
            code_sha="abc123",
            strategy_spec_path=spec,
            qc_audit_copy_path=tmp_path / "missing.py",
            qc_cloud_backtest_id="bt-1",
            account_id="DU111",
            start_date_ms=1_700_000_000_000,
            live_config={},
        )
    assert "qc_audit_copy_path" in str(exc.value)


def test_write_then_read_ledger_round_trips(tmp_path: Path) -> None:
    spec, qc_copy = _make_inputs(tmp_path)
    ledger = build_ledger(
        code_sha="abc123",
        strategy_spec_path=spec,
        qc_audit_copy_path=qc_copy,
        qc_cloud_backtest_id="bt-1",
        account_id="DU111",
        start_date_ms=1_700_000_000_000,
        live_config={"symbol": "SPY"},
    )
    out = tmp_path / "live_runs" / ledger.run_id / "run_ledger.json"
    write_ledger(out, ledger)

    loaded = read_ledger(out)
    assert loaded.run_id == ledger.run_id
    assert loaded.code_sha == ledger.code_sha
    assert loaded.strategy_spec_sha256 == ledger.strategy_spec_sha256
    assert loaded.qc_audit_copy_sha256 == ledger.qc_audit_copy_sha256
    assert loaded.live_config == {"symbol": "SPY"}


def test_write_ledger_produces_canonical_bytes(tmp_path: Path) -> None:
    """Ledger JSON bytes are deterministic — same inputs ⇒ same SHA-256.

    This is what § 6.5's ``run_ledger`` hash field in the daily Markdown
    relies on. If the JSON serialization order ever drifts, downstream
    hash verification breaks silently.
    """
    spec, qc_copy = _make_inputs(tmp_path)
    ledger = build_ledger(
        code_sha="abc123",
        strategy_spec_path=spec,
        qc_audit_copy_path=qc_copy,
        qc_cloud_backtest_id="bt-1",
        account_id="DU111",
        start_date_ms=1_700_000_000_000,
        live_config={"symbol": "SPY", "force_flat_minutes": 5},
    )
    p1 = tmp_path / "a" / "ledger.json"
    p2 = tmp_path / "b" / "ledger.json"
    write_ledger(p1, ledger)
    write_ledger(p2, ledger)
    assert p1.read_bytes() == p2.read_bytes()


def test_write_ledger_interrupted_replace_keeps_prior_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec, qc_copy = _make_inputs(tmp_path)
    ledger = build_ledger(
        code_sha="abc123",
        strategy_spec_path=spec,
        qc_audit_copy_path=qc_copy,
        qc_cloud_backtest_id="bt-1",
        account_id="DU111",
        start_date_ms=1_700_000_000_000,
        live_config={"symbol": "SPY"},
    )
    out = tmp_path / "live_runs" / ledger.run_id / "run_ledger.json"
    write_ledger(out, ledger)
    prior_bytes = out.read_bytes()

    changed = ledger.model_copy(update={"code_sha": "def456"})
    real_replace = run_ledger_module.os.replace

    def crash_before_publish(src: object, dst: object) -> None:
        if Path(dst) == out:
            raise OSError("simulated crash before ledger publish")
        real_replace(src, dst)

    with monkeypatch.context() as m:
        m.setattr(run_ledger_module.os, "replace", crash_before_publish)
        with pytest.raises(OSError, match="simulated crash"):
            write_ledger(out, changed)

    assert out.read_bytes() == prior_bytes
    assert read_ledger(out).code_sha == "abc123"
    assert not list(out.parent.glob(f".{out.name}.*.tmp"))


# ──────────────────── UI-0 identity binding (strategy_instance_id) ─────


def test_build_ledger_stores_strategy_instance_id_and_bumps_schema(tmp_path: Path) -> None:
    spec, qc_copy = _make_inputs(tmp_path)
    ledger = build_ledger(
        code_sha="abc123",
        strategy_spec_path=spec,
        qc_audit_copy_path=qc_copy,
        qc_cloud_backtest_id="bt-1",
        account_id="DU111",
        start_date_ms=1_700_000_000_000,
        live_config={"symbol": "SPY"},
        strategy_instance_id="spy-ema-paper-1",
    )
    assert ledger.strategy_instance_id == "spy-ema-paper-1"
    assert ledger.schema_version == "1.4"


def test_strategy_instance_id_not_in_run_id_hash(tmp_path: Path) -> None:
    """LOCKED decision: ``strategy_instance_id`` is persisted but NOT part
    of the ``run_id`` identity hash. Building with instance id "A", "B",
    or absent must yield a byte-identical ``run_id`` — proving existing
    run_ids, run directories, and fixtures stay valid (back-compat).
    """
    spec, qc_copy = _make_inputs(tmp_path)
    common = dict(
        code_sha="abc123",
        strategy_spec_path=spec,
        qc_audit_copy_path=qc_copy,
        qc_cloud_backtest_id="bt-1",
        account_id="DU111",
        start_date_ms=1_700_000_000_000,
        live_config={"symbol": "SPY"},
    )
    run_id_a = build_ledger(**common, strategy_instance_id="A").run_id
    run_id_b = build_ledger(**common, strategy_instance_id="B").run_id
    run_id_absent = build_ledger(**common).run_id

    assert run_id_a == run_id_b == run_id_absent


def test_read_legacy_1_0_ledger_without_field_defaults_empty(tmp_path: Path) -> None:
    """A hand-written schema-1.0 ledger (no ``strategy_instance_id`` key)
    reads cleanly: the field defaults to empty string, no error.
    """
    spec, qc_copy = _make_inputs(tmp_path)
    legacy = build_ledger(
        code_sha="abc123",
        strategy_spec_path=spec,
        qc_audit_copy_path=qc_copy,
        qc_cloud_backtest_id="bt-1",
        account_id="DU111",
        start_date_ms=1_700_000_000_000,
        live_config={"symbol": "SPY"},
    )
    payload = legacy.model_dump(mode="json")
    payload["schema_version"] = "1.0"
    del payload["strategy_instance_id"]
    out = tmp_path / "legacy" / "run_ledger.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload), encoding="utf-8")

    loaded = read_ledger(out)
    assert loaded.schema_version == "1.0"
    assert loaded.strategy_instance_id == ""


def test_write_read_round_trips_strategy_instance_id(tmp_path: Path) -> None:
    spec, qc_copy = _make_inputs(tmp_path)
    ledger = build_ledger(
        code_sha="abc123",
        strategy_spec_path=spec,
        qc_audit_copy_path=qc_copy,
        qc_cloud_backtest_id="bt-1",
        account_id="DU111",
        start_date_ms=1_700_000_000_000,
        live_config={"symbol": "SPY"},
        strategy_instance_id="spy-ema-paper-1",
    )
    out = tmp_path / "live_runs" / ledger.run_id / "run_ledger.json"
    write_ledger(out, ledger)

    loaded = read_ledger(out)
    assert loaded.strategy_instance_id == "spy-ema-paper-1"
    assert loaded.schema_version == "1.4"
    assert loaded.run_id == ledger.run_id


def test_default_ledger_is_latest_schema() -> None:
    """A freshly constructed ledger (no instance id / strategy key, no sizing)
    defaults to the current schema with empty bindings — the new baseline.
    """
    ledger = LiveRunLedger(
        run_id="x" * 64,
        code_sha="abc",
        strategy_spec_path="/tmp/s.json",
        strategy_spec_sha256="d" * 64,
        qc_audit_copy_path="/tmp/q.py",
        qc_audit_copy_sha256="e" * 64,
        qc_cloud_backtest_id="bt-1",
        account_id="DU111",
        start_date_ms=1_700_000_000_000,
        live_config={},
    )
    assert ledger.schema_version == "1.4"
    assert ledger.strategy_instance_id == ""
    assert ledger.strategy_key == ""
    # ADR 0009 — defaults for an empty/legacy live_config.
    assert ledger.governed_by == "live_config"
    assert ledger.sizing_provenance == "live_override"


# ──────────────────── algorithm-module binding (strategy_key, #416) ────


def test_build_ledger_stores_strategy_key(tmp_path: Path) -> None:
    spec, qc_copy = _make_inputs(tmp_path)
    ledger = build_ledger(
        code_sha="abc123",
        strategy_spec_path=spec,
        qc_audit_copy_path=qc_copy,
        qc_cloud_backtest_id="bt-1",
        account_id="DU111",
        start_date_ms=1_700_000_000_000,
        live_config={"symbol": "SPY"},
        strategy_key="spy_ema_crossover",
    )
    assert ledger.strategy_key == "spy_ema_crossover"
    assert ledger.schema_version == "1.4"


def test_strategy_key_not_in_run_id_hash(tmp_path: Path) -> None:
    """``strategy_key`` is persisted but NOT part of the ``run_id`` identity
    hash — adding it must keep existing run_ids/dirs/fixtures byte-identical.
    """
    spec, qc_copy = _make_inputs(tmp_path)
    common = dict(
        code_sha="abc123",
        strategy_spec_path=spec,
        qc_audit_copy_path=qc_copy,
        qc_cloud_backtest_id="bt-1",
        account_id="DU111",
        start_date_ms=1_700_000_000_000,
        live_config={"symbol": "SPY"},
    )
    run_id_x = build_ledger(**common, strategy_key="spy_ema_crossover").run_id
    run_id_y = build_ledger(**common, strategy_key="rsi_mean_reversion").run_id
    run_id_absent = build_ledger(**common).run_id

    assert run_id_x == run_id_y == run_id_absent


def test_start_defaults_not_in_run_id_hash(tmp_path: Path) -> None:
    spec, qc_copy = _make_inputs(tmp_path)
    common = dict(
        code_sha="abc123",
        strategy_spec_path=spec,
        qc_audit_copy_path=qc_copy,
        qc_cloud_backtest_id="bt-1",
        account_id="DU111",
        start_date_ms=1_700_000_000_000,
        live_config={"symbol": "SPY"},
        strategy_key="deployment_validation",
    )
    paper_run = build_ledger(
        **common,
        start_defaults=LiveRunStartDefaults(
            strategy="deployment_validation",
            readonly=False,
            hydrate_policy="optional",
            max_orders_per_day=3,
        ),
    )
    shadow_run = build_ledger(
        **common,
        start_defaults=LiveRunStartDefaults(
            strategy="deployment_validation",
            readonly=True,
            hydrate_policy="require",
            max_orders_per_day=2_000,
        ),
    )

    assert paper_run.run_id == shadow_run.run_id
    assert paper_run.start_defaults is not None
    assert paper_run.start_defaults.max_orders_per_day == 3


def test_read_legacy_1_1_ledger_without_strategy_key_defaults_empty(tmp_path: Path) -> None:
    """A schema-1.1 ledger (no ``strategy_key`` key) reads cleanly: the field
    defaults to empty string, no error.
    """
    spec, qc_copy = _make_inputs(tmp_path)
    legacy = build_ledger(
        code_sha="abc123",
        strategy_spec_path=spec,
        qc_audit_copy_path=qc_copy,
        qc_cloud_backtest_id="bt-1",
        account_id="DU111",
        start_date_ms=1_700_000_000_000,
        live_config={"symbol": "SPY"},
        strategy_instance_id="spy-ema-paper-1",
    )
    payload = legacy.model_dump(mode="json")
    payload["schema_version"] = "1.1"
    del payload["strategy_key"]
    out = tmp_path / "legacy11" / "run_ledger.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload), encoding="utf-8")

    loaded = read_ledger(out)
    assert loaded.schema_version == "1.1"
    assert loaded.strategy_key == ""


def test_write_read_round_trips_strategy_key(tmp_path: Path) -> None:
    spec, qc_copy = _make_inputs(tmp_path)
    ledger = build_ledger(
        code_sha="abc123",
        strategy_spec_path=spec,
        qc_audit_copy_path=qc_copy,
        qc_cloud_backtest_id="bt-1",
        account_id="DU111",
        start_date_ms=1_700_000_000_000,
        live_config={"symbol": "SPY"},
        strategy_key="spy_ema_crossover",
    )
    out = tmp_path / "live_runs" / ledger.run_id / "run_ledger.json"
    write_ledger(out, ledger)

    loaded = read_ledger(out)
    assert loaded.strategy_key == "spy_ema_crossover"
    assert loaded.run_id == ledger.run_id


# ──────────────────── ADR 0009 sizing stamps + run_id stability ─────


def test_build_ledger_derives_governed_by_from_sizing_kind(tmp_path: Path) -> None:
    spec, qc_copy = _make_inputs(tmp_path)
    common = dict(
        code_sha="abc123",
        strategy_spec_path=spec,
        qc_audit_copy_path=qc_copy,
        qc_cloud_backtest_id="bt-1",
        account_id="DU111",
        start_date_ms=1_700_000_000_000,
    )
    canary = build_ledger(
        live_config={"sizing": {"kind": "FixedShares", "value": 1}}, **common
    )
    explicit = build_ledger(live_config={"sizing": {"kind": "StrategyExplicit"}}, **common)
    legacy = build_ledger(live_config={}, **common)

    assert canary.governed_by == "live_config"
    # No allow-list at this tmp_path, so the lookup yields cannot_prove and
    # the fail-closed default `live_override` lands on the ledger.
    assert canary.sizing_provenance == "live_override"
    assert explicit.governed_by == "strategy_explicit"
    assert legacy.governed_by == "live_config"
    assert legacy.sizing_provenance == "live_override"


def test_build_ledger_rejects_empty_sizing_payload(tmp_path: Path) -> None:
    """A deploy with ``live_config = {"sizing": {}}`` would otherwise persist a
    ledger that fails to start (``_live_config_from_ledger`` parses on key
    presence). Fail fast at build time so the deploy boundary surfaces the
    same error rather than producing an unstartable run.
    """
    spec, qc_copy = _make_inputs(tmp_path)
    with pytest.raises(ValueError, match=r"invalid live_config\.sizing"):
        build_ledger(
            code_sha="abc123",
            strategy_spec_path=spec,
            qc_audit_copy_path=qc_copy,
            qc_cloud_backtest_id="bt-1",
            account_id="DU111",
            start_date_ms=1_700_000_000_000,
            live_config={"sizing": {}},
        )


def test_build_ledger_derives_reference_native_on_allow_list_match(tmp_path: Path) -> None:
    """ADR 0009 § 3 — when ``live_config.sizing`` matches the registered rule
    AND the audit copy's sha re-verifies, the engine stamps ``reference_native``.
    Every other outcome falls to the fail-closed ``live_override``.
    """
    import hashlib
    import json as _json

    # Build a repo layout the lookup can resolve.
    repo_root = tmp_path / "repo"
    spec = repo_root / "spec.json"
    qc_rel = "references/qc-shadow/Foo.py"
    qc_copy = repo_root / qc_rel
    qc_copy.parent.mkdir(parents=True, exist_ok=True)
    spec.parent.mkdir(parents=True, exist_ok=True)
    spec.write_text('{"strategy": "x"}', encoding="utf-8")
    content = "# canonical"
    qc_copy.write_text(content, encoding="utf-8")
    sha = hashlib.sha256(content.encode()).hexdigest()
    allow_list = repo_root / "docs/references/audit-copy-sizing-allow-list.json"
    allow_list.parent.mkdir(parents=True, exist_ok=True)
    allow_list.write_text(
        _json.dumps(
            [
                {
                    "audit_copy_path": qc_rel,
                    "audit_copy_sha256": sha,
                    "rule": {"kind": "SetHoldings", "fraction": "1.0"},
                    "registered_at_ms": 0,
                    "registered_by": "x",
                }
            ]
        ),
        encoding="utf-8",
    )

    matched = build_ledger(
        code_sha="abc",
        strategy_spec_path=spec,
        qc_audit_copy_path=qc_copy,
        qc_cloud_backtest_id="bt-1",
        account_id="DU111",
        start_date_ms=0,
        live_config={"sizing": {"kind": "SetHoldings", "fraction": "1.0"}},
        audit_copy_allow_list_root=repo_root,
    )
    assert matched.sizing_provenance == "reference_native"

    mismatched = build_ledger(
        code_sha="abc",
        strategy_spec_path=spec,
        qc_audit_copy_path=qc_copy,
        qc_cloud_backtest_id="bt-1",
        account_id="DU111",
        start_date_ms=0,
        live_config={"sizing": {"kind": "FixedShares", "value": 1}},
        audit_copy_allow_list_root=repo_root,
    )
    assert mismatched.sizing_provenance == "live_override"


def test_sizing_changes_run_id_via_live_config_hash(tmp_path: Path) -> None:
    """ADR 0009 — live_config.sizing is hashed into run_id through live_config,
    so two deploys differing only in sizing kind mint different run_ids."""
    spec, qc_copy = _make_inputs(tmp_path)
    common = dict(
        code_sha="abc123",
        strategy_spec_path=spec,
        qc_audit_copy_path=qc_copy,
        qc_cloud_backtest_id="bt-1",
        account_id="DU111",
        start_date_ms=1_700_000_000_000,
    )
    canary_id = build_ledger(
        live_config={"sizing": {"kind": "FixedShares", "value": 1}}, **common
    ).run_id
    legacy_id = build_ledger(live_config={}, **common).run_id
    assert canary_id != legacy_id, (
        "Safe-canary must mint a different run_id than an empty live_config "
        "so a sizing-aware deploy never collides with a legacy empty-config run."
    )


def test_governed_by_and_provenance_not_in_run_id_hash(tmp_path: Path) -> None:
    """ADR 0009 — the engine-derived stamps are NOT hashed; only the operator
    choice (live_config.sizing) is. Two ledgers with the same live_config but
    different stamps would never exist in practice, but the hash must not
    depend on them, so a future stamp-default change leaves run_ids stable.
    """
    payload = {
        "code_sha": "abc123",
        "strategy_spec_sha256": "deadbeef",
        "qc_audit_copy_sha256": "cafe",
        "qc_cloud_backtest_id": "bt-1",
        "account_id": "DU111",
        "start_date_ms": 1_700_000_000_000,
        "live_config": {"sizing": {"kind": "FixedShares", "value": 1}},
    }
    # compute_run_id signature does NOT take governed_by / sizing_provenance —
    # this test pins that contract explicitly.
    assert "governed_by" not in payload
    assert "sizing_provenance" not in payload
    assert len(compute_run_id(**payload)) == 64


def test_legacy_1_2_ledger_reads_with_default_sizing_stamps(tmp_path: Path) -> None:
    """A 1.2 ledger persisted before ADR 0009 has no governed_by or
    sizing_provenance keys. Reading it falls to the defaults, never errors."""
    spec, qc_copy = _make_inputs(tmp_path)
    legacy = build_ledger(
        code_sha="abc123",
        strategy_spec_path=spec,
        qc_audit_copy_path=qc_copy,
        qc_cloud_backtest_id="bt-1",
        account_id="DU111",
        start_date_ms=1_700_000_000_000,
        live_config={"symbol": "SPY"},
        strategy_key="spy_ema_crossover",
    )
    payload = legacy.model_dump(mode="json")
    payload["schema_version"] = "1.2"
    del payload["governed_by"]
    del payload["sizing_provenance"]
    out = tmp_path / "legacy12" / "run_ledger.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload), encoding="utf-8")

    loaded = read_ledger(out)
    assert loaded.schema_version == "1.2"
    assert loaded.governed_by == "live_config"
    assert loaded.sizing_provenance == "live_override"
