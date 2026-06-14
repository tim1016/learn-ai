"""Tests for the strategy registry and the list_engine_strategies handler.

This is the contract between the Engine Lab frontend strategy picker and
the Python backend registry. After the 2026-04-18 metadata push, every
StrategyRegistration must populate `algorithm_pseudocode` (a pseudocode
snippet) and `gotchas` (a non-empty list of implementation / parity
traps). If either is missing the frontend renders an empty section.

These tests protect:
    1. The `orb` strategy is registered and wire-able end-to-end.
    2. Every registered strategy ships both `algorithm_pseudocode` and
       `gotchas` — populating them is a must-do for every new strategy.
    3. The ORB gotcha list flags the one-trade-per-day invariant (the
       QQQ validation study's primary finding).
    4. Each params_schema round-trips cleanly through JSON so the
       frontend's dynamic form renderer can consume it.

The handler is called directly rather than through a FastAPI TestClient,
because spinning up the full app pulls in Polygon/REST dependencies that
aren't needed to test the registry contract.
"""

from __future__ import annotations

import json

EXPECTED_STRATEGY_KEYS = {
    # VCR-0004 / Phase 2 — registry keys are now module names so the runner
    # can import every registered strategy by ``app.engine.strategy.algorithms.{key}``.
    "spy_ema_crossover",
    "sma_crossover",
    "daily_sma_crossover",
    "rsi_mean_reversion",
    "spy_orb",
    "deployment_validation",
    "spy_ema_crossover_options",
    "spy_strategy_a",
    "spy_strategy_b",
    "spy_strategy_c",
}


def _list_strategies():
    """Call the router's list_engine_strategies handler directly.

    Returns a list of dicts (the Pydantic StrategyInfo model_dumped).
    """
    from app.routers.engine import list_engine_strategies

    return [s.model_dump() for s in list_engine_strategies()]


def test_orb_is_registered_with_correct_metadata():
    """The orb strategy is present and carries the expected metadata.

    VCR-0004 / Phase 2: the registry key is now the module name (``spy_orb``)
    so the runner imports the same file the dropdown advertises."""
    strategies = _list_strategies()
    names = {s["name"] for s in strategies}
    assert "spy_orb" in names, f"spy_orb strategy missing from registry; saw: {sorted(names)}"

    orb = next(s for s in strategies if s["name"] == "spy_orb")
    assert orb["display_name"] == "Opening Range Breakout"
    assert "minute" in orb["supported_resolutions"]

    # OrbParams contract — params_schema must expose the five knobs the
    # frontend form renders against.
    props = orb["params_schema"]["properties"]
    for expected in ("symbol", "orb_bars", "hold_bars", "min_range_pct", "max_range_pct"):
        assert expected in props, f"orb params_schema missing {expected!r}"


def test_deployment_validation_is_registered_with_fixed_rule_metadata():
    strategies = _list_strategies()
    names = {s["name"] for s in strategies}
    assert "deployment_validation" in names

    strategy = next(s for s in strategies if s["name"] == "deployment_validation")
    assert strategy["display_name"] == "Deployment Validation"
    assert "minute" in strategy["supported_resolutions"]
    assert set(strategy["params_schema"]["properties"]) == {"symbol"}
    combined = " ".join(strategy["gotchas"]).lower()
    assert "next_bar_open" in combined


def test_deployment_validation_has_matching_spec_fixture():
    from app.routers.spec_strategy import list_fixtures

    fixtures = {f.name: f for f in list_fixtures()}
    assert "deployment_validation" in fixtures
    assert fixtures["deployment_validation"].path.endswith("deployment_validation.spec.json")


def test_all_registered_strategies_have_algorithm_and_gotchas():
    """Every strategy must populate the two structured metadata fields.

    An empty string or empty list passes the Pydantic type check but
    renders as an empty section in the UI — strictly worse than not
    having the field at all. So we assert non-emptiness.
    """
    strategies = _list_strategies()
    names = {s["name"] for s in strategies}
    assert names >= EXPECTED_STRATEGY_KEYS, (
        f"registry regressed — at least one expected strategy is missing: {EXPECTED_STRATEGY_KEYS - names}"
    )

    missing_algo = [s["name"] for s in strategies if not s.get("algorithm_pseudocode", "").strip()]
    missing_gotchas = [s["name"] for s in strategies if not s.get("gotchas")]

    assert not missing_algo, (
        f"strategies missing algorithm_pseudocode: {missing_algo}.  "
        f"Populate the field in app/routers/engine.py _STRATEGY_REGISTRY."
    )
    assert not missing_gotchas, (
        f"strategies missing gotchas: {missing_gotchas}.  "
        f"Populate the field in app/routers/engine.py _STRATEGY_REGISTRY."
    )


def test_orb_gotchas_include_traded_today_guard():
    """The ORB gotcha list must flag the one-trade-per-day invariant.

    This is the bug the QQQ validation study surfaced; having it
    permanently in the UI gotcha list is how we prevent the next
    porter from overlooking it.
    """
    orb = next(s for s in _list_strategies() if s["name"] == "spy_orb")
    combined = " ".join(orb["gotchas"]).lower()
    assert "traded_today" in combined or "one trade per day" in combined, (
        "ORB gotcha list should document the one-trade-per-day guard — the QQQ validation study's primary finding."
    )


def test_params_schema_is_round_trippable_json():
    """params_schema must be plain JSON the frontend can consume."""
    for s in _list_strategies():
        dumped = json.dumps(s["params_schema"])
        assert json.loads(dumped) == s["params_schema"]


def test_build_callable_constructs_orb_with_default_params():
    """Smoke check: the registered build lambda accepts a default-params
    instance and returns a SpyOpeningRangeBreakout.

    This catches the common refactor regression of renaming a field in
    OrbParams without updating the build lambda's type: ignore call.
    """
    from app.routers.engine import _STRATEGY_REGISTRY

    reg = _STRATEGY_REGISTRY["spy_orb"]
    default_params = reg.param_schema()
    instance = reg.build(default_params)
    assert instance.__class__.__name__ == "SpyOpeningRangeBreakout"
    # The symbol override must round-trip through the build step
    qqq_params = reg.param_schema(symbol="QQQ")
    qqq_instance = reg.build(qqq_params)
    assert qqq_instance._symbol_name == "QQQ"


# ────────────────── VCR-0004 / Phase 2 — module-name contract ─────────


def test_every_registered_strategy_can_be_imported_by_key():
    """The registry key IS the module name — the runner imports by it. If a
    registered key cannot be imported, the dropdown advertises a strategy that
    cannot start. This was the smoking gun for VCR-0004 (7 of 10 dropdown items
    broken)."""
    from importlib import import_module

    from app.routers.engine import _STRATEGY_REGISTRY

    failures = []
    for key in _STRATEGY_REGISTRY:
        try:
            import_module(f"app.engine.strategy.algorithms.{key}")
        except ImportError as exc:
            failures.append(f"{key}: {exc}")
    assert not failures, (
        "Registry keys must match algorithm module names so the runner can "
        "import them. Failing keys: " + "; ".join(failures)
    )


def test_every_registered_strategy_has_explicit_class_name():
    """``StrategyRegistration.class_name`` retires the ``<PascalKey>Algorithm``
    convention. Every entry names its class explicitly so a future class rename
    does not silently break the runner's class lookup."""
    from app.routers.engine import _STRATEGY_REGISTRY

    missing = [key for key, reg in _STRATEGY_REGISTRY.items() if not getattr(reg, "class_name", "")]
    assert not missing, (
        f"Strategies missing class_name: {missing}. Phase 2 / VCR-0004 — "
        f"populate StrategyRegistration.class_name for every entry."
    )


def test_every_registered_class_name_resolves_against_its_module():
    """The runner does ``getattr(module, registration.class_name)``. Every
    registered ``class_name`` must resolve to a class in its module."""
    from importlib import import_module

    from app.routers.engine import _STRATEGY_REGISTRY

    failures = []
    for key, reg in _STRATEGY_REGISTRY.items():
        try:
            module = import_module(f"app.engine.strategy.algorithms.{key}")
        except ImportError as exc:
            failures.append(f"{key} module import: {exc}")
            continue
        cls = getattr(module, reg.class_name, None)
        if cls is None:
            failures.append(f"{key}: module has no class {reg.class_name!r}")
    assert not failures, (
        "Registered class_name must resolve in its module. Failing entries: "
        + "; ".join(failures)
    )


def test_deployment_validation_class_name_is_consecutive_green():
    """The ``DeploymentValidationAlgorithm`` alias is retired. The registry
    names the real class (``DeploymentValidationConsecutiveGreen``)."""
    from app.routers.engine import _STRATEGY_REGISTRY

    reg = _STRATEGY_REGISTRY["deployment_validation"]
    assert reg.class_name == "DeploymentValidationConsecutiveGreen"


def test_deployment_validation_alias_no_longer_exists():
    """``DeploymentValidationAlgorithm = DeploymentValidationConsecutiveGreen``
    in ``deployment_validation.py`` was the convention paper-over. Delete it
    along with the convention itself; the registry's ``class_name`` is the
    sole source of truth."""
    from app.engine.strategy.algorithms import deployment_validation

    assert not hasattr(deployment_validation, "DeploymentValidationAlgorithm"), (
        "DeploymentValidationAlgorithm alias must be removed — the registry's "
        "class_name names DeploymentValidationConsecutiveGreen directly."
    )


if __name__ == "__main__":
    # Allow `python -m app.engine.tests.test_engine_strategies_endpoint` style.
    import sys

    tests = [
        test_orb_is_registered_with_correct_metadata,
        test_deployment_validation_has_matching_spec_fixture,
        test_all_registered_strategies_have_algorithm_and_gotchas,
        test_orb_gotchas_include_traded_today_guard,
        test_params_schema_is_round_trippable_json,
        test_build_callable_constructs_orb_with_default_params,
    ]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"PASS: {t.__name__}")
        except AssertionError as e:
            print(f"FAIL: {t.__name__}\n       {e}")
            failed += 1
    sys.exit(1 if failed else 0)
