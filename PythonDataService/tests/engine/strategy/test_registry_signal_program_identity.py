"""Structural guard: a Signal Program registration must never silently
inherit another registration's sealed identity via ``dataclasses.replace``.

Issue #1728 defect 1: ``_STRATEGY_REGISTRY["ema_crossover_2_bps"]`` and
``_STRATEGY_REGISTRY["spy_ema_crossover"]`` were built with
``dataclasses.replace(_ema_signal_registration, ...)`` in
``app/engine/strategy/registry.py``. ``replace()`` shallow-copies every field
not explicitly overridden, including ``signal_program_contract`` and
``signal_program_factory`` — so two strategies with genuinely different math
(a relative basis-point gap vs. EMA's absolute-price gap; a
``action_plan_contract="none"`` legacy wrapper vs. the canonical program)
ended up claiming EMA's qualification receipt
(``program_version``, ``golden_trace_root``, ``validated_settings``,
``artifact_paths``) as their own.

``prove_running_program_build``
(``app/services/signal_program_admission.py``) keys receipt lookup off
``binding.strategy_key``, and ``app/data/signal_program_build_receipts.json``
only ever held a receipt for ``ema_crossover_signal`` — so no lookup for
either derived key could ever match. Every bot on either strategy got
``PROGRAM_BUILD_UNPROVEN`` and could never Start or Resume.

These tests make the *class* of bug impossible, not just this one instance:
they fail if any two distinct registry keys ever again end up sharing a
contract object, sharing a ``(program_version, golden_trace_root)`` pair, or
carrying a factory/contract pairing that has drifted apart.
"""

from __future__ import annotations

import pytest

from app.engine.indicators.base import BarIndicator, Indicator
from app.engine.indicators.ema import ExponentialMovingAverage
from app.engine.indicators.rsi import RelativeStrengthIndex
from app.engine.strategy.programs.ema_crossover_signal import EmaCrossoverSignalParams
from app.engine.strategy.registry import _STRATEGY_REGISTRY
from app.engine.strategy.signal_program import SignalSession
from tests._helpers.signal_program import (
    bind_strategy_context,
    indexed_bucket,
    sealed_series_points,
    series_point_id,
)


def test_each_sealed_signal_program_identity_is_unique_to_its_registry_key() -> None:
    contracts_by_key = {
        key: reg.signal_program_contract for key, reg in _STRATEGY_REGISTRY.items() if reg.signal_program_contract is not None
    }
    assert contracts_by_key, "expected at least one registered Signal Program contract"

    seen_object_owner: dict[int, str] = {}
    seen_identity_owner: dict[tuple[str, str], str] = {}
    for key, contract in contracts_by_key.items():
        object_owner = seen_object_owner.get(id(contract))
        assert object_owner is None, (
            f"'{key}' shares its signal_program_contract object with '{object_owner}' — "
            "dataclasses.replace() shallow-copies the contract field onto every "
            "derived registration unless it is explicitly overridden. Give this "
            "registration its own SignalProgramContract, or set "
            "signal_program_contract=None if it has not been through its own "
            "golden qualification."
        )
        seen_object_owner[id(contract)] = key

        identity = (contract.program_version, contract.golden_trace_root)
        identity_owner = seen_identity_owner.get(identity)
        assert identity_owner is None, (
            f"'{key}' claims the same qualified (program_version, golden_trace_root) "
            f"as '{identity_owner}': {identity!r}. Every sealed registration must have "
            "a trace root of its own — sharing one lets a bot deployed on this key "
            "pass build-proof admission using another program's golden-qualification "
            "evidence for bytes that were never actually re-run against this "
            "strategy's own math."
        )
        seen_identity_owner[identity] = key


def test_signal_program_contract_and_factory_are_set_or_cleared_together() -> None:
    """``StrategyRegistration.signal_program_contract``'s own docstring states the
    invariant: "A factory without a contract is an unqualified program and cannot
    be sealed for Start/Resume." Defect 1 was the inverse failure mode — both were
    present, but neither belonged to the registration carrying them. Catching drift
    in either direction keeps that pairing honest.
    """
    for key, reg in _STRATEGY_REGISTRY.items():
        has_contract = reg.signal_program_contract is not None
        has_factory = reg.signal_program_factory is not None
        assert has_contract == has_factory, (
            f"'{key}' has signal_program_contract={'set' if has_contract else 'None'} but "
            f"signal_program_factory={'set' if has_factory else 'None'} — a registered "
            "Signal Program must set both or neither."
        )


def test_every_registered_strategy_decouples_signal_from_traded_asset() -> None:
    """Every registration owns a Signal Program. No exceptions, by design.

    The platform rule is that a strategy separates *when* to act (the signal)
    from *what* is traded (the asset the execution boundary selects) -- see
    ``app.engine.strategy.signal_intent.SignalIntent``, which carries neither
    a symbol nor a quantity. A registration without a
    ``signal_program_factory`` decides *and* names its own traded symbol,
    which is exactly the coupling this rule exists to remove.

    There is deliberately no allowlist. The sweep that established this rule
    removed the last five coupled registrations rather than excusing them, so
    the honest assertion is that the set is empty -- an allowlist here would
    only be a place for the next one to hide.
    """
    coupled = sorted(
        key for key, reg in _STRATEGY_REGISTRY.items() if reg.signal_program_factory is None
    )

    assert coupled == []


def test_every_factory_built_program_carries_its_registration_identity() -> None:
    """The constructed program's identity must equal its registration's.

    ``program_version`` reaches two independent consumers by two independent
    routes: the factory stamps it onto ``SignalSession``, where it is hashed
    into every ``evaluation_id``; the contract declares it, where it becomes
    the sealed build-proof identity. Neither hash looks wrong on its own, so
    a drift between them yields a bot whose decision traces claim a
    different program than its seal -- visible only as an unexplained
    mismatch far downstream. ``program_key`` has the same exposure against
    the registry key that ``prove_running_program_build`` looks receipts up
    by.

    Checking the relationship for *every* registered program (rather than
    pinning EMA's literals) is what makes this survive slice 5: each program
    promoted through the governed seam is covered the moment it is
    registered, with no per-program test to remember.
    """
    sealed = [(key, reg) for key, reg in _STRATEGY_REGISTRY.items() if reg.signal_program_factory is not None]
    assert sealed, "expected at least one registered Signal Program factory"

    for key, reg in sealed:
        contract = reg.signal_program_contract
        assert contract is not None  # paired invariant, asserted by the test above
        program = reg.signal_program_factory(reg.param_schema())  # type: ignore[misc]

        assert program.session.program_key == key, (
            f"'{key}' builds a session claiming program_key="
            f"{program.session.program_key!r}. prove_running_program_build() looks "
            "the build receipt up by the registry key, so a session that names a "
            "different key traces its decisions under a program whose bytes were "
            "never qualified for it."
        )
        assert program.session.program_version == contract.program_version, (
            f"'{key}' builds a session at program_version="
            f"{program.session.program_version!r} but its contract declares "
            f"{contract.program_version!r}. Declare the identity once and "
            "reference it from both the factory and the contract."
        )


def test_registry_protocol_and_parameter_schema_versions_mirror_their_one_declaration() -> None:
    """Sealed-completeness fix: ``protocol_version`` and
    ``parameter_schema_version`` are each declared exactly once — on
    ``SignalSession``/``EmaCrossoverSignalParams`` respectively —
    and the registry contract only ever mirrors that constant. This is the
    guard that keeps the mirror from drifting the way ``program_version``'s
    hand-duplicated literal already can: catching it here, not only via a
    downstream hash mismatch, names exactly which constant went stale.
    """
    contract = _STRATEGY_REGISTRY["ema_crossover_signal"].signal_program_contract
    assert contract is not None

    assert contract.protocol_version == SignalSession.PROTOCOL_VERSION
    # The static value is the reference point's (#2696); any other lengths seal the full schema's version.
    assert contract.parameter_schema_version == EmaCrossoverSignalParams.REFERENCE_PARAMETER_SCHEMA_VERSION
    extended = EmaCrossoverSignalParams.model_validate({"symbol": "SPY", "fast_period": 7})
    assert contract.resolved_parameter_schema_version(extended) == EmaCrossoverSignalParams.PARAMETER_SCHEMA_VERSION


def test_registry_signal_series_periods_match_the_constructed_indicators() -> None:
    """The registry's sealed ``signals`` periods must describe the real,
    constructed indicators, not a value that can silently drift out of sync
    with ``EmaCrossoverSignalAlgorithm.initialize()``. This directly checks
    the constructed indicator objects rather than relying only on the
    golden-trace test to notice a period change indirectly.
    """
    contract = _STRATEGY_REGISTRY["ema_crossover_signal"].signal_program_contract
    assert contract is not None
    periods_by_name = {series.name: series.period for series in contract.signals}
    warmup_by_name = {series.name: series.warmup_bars for series in contract.signals}

    # Constructed the exact same way EmaCrossoverSignalAlgorithm.initialize()
    # builds them at the default point ("EMA5", 5), ("EMA10", 10),
    # ("RSI14", 14) — a narrow check of the static period constants. The
    # parameter-resolved series are checked against real constructed
    # indicators in test_ema_crossover_signal_lengths.py (#2696).
    ema_fast = ExponentialMovingAverage("EMA5", 5)
    ema_slow = ExponentialMovingAverage("EMA10", 10)
    rsi = RelativeStrengthIndex("RSI14", 14)

    assert periods_by_name["ema_fast"] == ema_fast.period
    assert periods_by_name["ema_slow"] == ema_slow.period
    assert periods_by_name["rsi"] == rsi.period
    # is_ready fires at samples >= period for EMA, period + 1 for RSI
    # (app/engine/indicators/base.py vs. RelativeStrengthIndex's override).
    assert warmup_by_name["ema_fast"] == ema_fast.period
    assert warmup_by_name["ema_slow"] == ema_slow.period
    assert warmup_by_name["rsi"] == rsi.period + 1


_DECISION_BAR_MS = 15 * 60_000


def _bars_until_ready(indicator: Indicator | BarIndicator) -> int:
    """How many decision bars a freshly built indicator takes to turn ready."""
    for count in range(1, 2_000):
        bar = indexed_bucket("SPY", count, _DECISION_BAR_MS, str(100 + count % 7))
        if isinstance(indicator, BarIndicator):
            indicator.update(bar)
        else:
            indicator.update(bar.end_ms, bar.close)
        if indicator.is_ready:
            return count
    raise AssertionError(f"{indicator.name} never became ready")


# ema_crossover_signal keeps its indicators under their reference-length names
# (``_ema5``); test_ema_crossover_signal_lengths.py holds it to the same rule.
_SERIES_POINTS = [point for point in sealed_series_points() if point[0] != "ema_crossover_signal"]


@pytest.mark.parametrize(("program_key", "overrides"), _SERIES_POINTS, ids=series_point_id)
def test_resolved_series_are_exactly_the_indicators_these_parameters_build(
    program_key: str, overrides: dict[str, int]
) -> None:
    """A seal names what runs (#2796): each series its parameters resolve is
    one indicator the program built from them constructs -- the same period,
    ready on the same bar -- and the program constructs no other. Each series
    is held as ``_<name>`` on its strategy.
    """
    registration = _STRATEGY_REGISTRY[program_key]
    contract = registration.signal_program_contract
    assert contract is not None and registration.signal_program_factory is not None
    params = registration.param_schema(**{**contract.validated_settings, **overrides})
    strategy = registration.signal_program_factory(params).strategy
    bind_strategy_context(strategy)
    built = {name: value for name, value in vars(strategy).items() if isinstance(value, Indicator | BarIndicator)}

    series = {f"_{entry.name}": entry for entry in contract.resolved_signals(params)}

    assert set(series) == set(built)
    for name, indicator in built.items():
        assert series[name].period == indicator.period, name
        assert series[name].warmup_bars == _bars_until_ready(indicator), name


def test_static_series_and_exit_rule_describe_each_program_s_default_point() -> None:
    """A contract's static ``signals``/``exit_eligibility``/``numerical_provenance`` are its default point.

    A program whose periods or hold are parameters resolves them per seal
    (#2696, #2796); its static values must still be what the default
    parameters resolve to, so a seal at the default point is byte-identical
    to one made before they were resolved.
    """
    for key, registration in _STRATEGY_REGISTRY.items():
        contract = registration.signal_program_contract
        if contract is None:
            continue
        defaults = registration.param_schema()

        assert contract.resolved_signals(defaults) == contract.signals, key
        assert contract.resolved_exit_eligibility(defaults) == contract.exit_eligibility, key
        assert contract.resolved_numerical_provenance(defaults) == contract.numerical_provenance, key


def test_validated_settings_a_dump_omits_are_their_schema_defaults() -> None:
    """``registry_point_matches`` reads a name absent from a dump as its validated value.

    That is sound only while every validated setting a canonical dump can omit
    (an identity-neutral default, #2696) equals the schema default the omitted
    name runs at. A validated point off the default would otherwise be claimed
    as covered by every default deploy.
    """
    checked = 0
    for key, registration in _STRATEGY_REGISTRY.items():
        contract = registration.signal_program_contract
        if contract is None:
            continue
        symbol = contract.validated_symbols[0] if contract.validated_symbols else registration.param_schema().symbol
        dumped = registration.param_schema.model_validate({**contract.validated_settings, "symbol": symbol}).model_dump(
            mode="json"
        )
        fields = registration.param_schema.model_fields
        for name, value in contract.validated_settings.items():
            if name not in dumped:
                checked += 1
                assert value == fields[name].default, (key, name)

    assert checked, "expected at least one identity-neutral validated setting (EMA's lengths)"


def test_validated_against_only_names_evidence_that_actually_exists() -> None:
    """A sealed contract's ``validated_against`` is audit metadata: it names
    the evidence a reader is supposed to be able to go read. If it names a
    test that was deleted or renamed, the seal still verifies and every test
    still passes -- the receipt just quietly points at nothing.

    This is a real regression, not a hypothetical: the six per-program
    ``test_validated_*_settings_corpus_has_a_pinned_trace_root`` functions
    these fields used to name were collapsed into
    ``test_signal_program_qualification_matrix.py``'s parameterized node, and
    every one of the six ``validated_against`` strings kept naming the
    deleted function. Nothing failed, because nothing checked.

    Resolving each named path (and, for a ``file::test`` node id, the test
    function itself, via AST rather than a pytest sub-run) makes the class of
    bug impossible instead of fixing the six instances.
    """
    import ast
    import re
    from pathlib import Path

    service_root = Path(__file__).resolve().parents[3]
    repo_root = service_root.parent

    def _resolve(candidate: str) -> Path | None:
        for root in (service_root, repo_root):
            path = root / candidate
            if path.exists():
                return path
        return None

    # Matches "a/b/c.py", "a/b/c.md" and "a/b/c.py::test_name[param]".
    # Requires a real "/" and a concrete file extension, so surrounding prose
    # ("(gate-wiring unit tests)", "ENG-008", "level/edge entry/exit") cannot
    # be mistaken for a path. Bare directory references are deliberately not
    # validated -- they are too ambiguous to distinguish from prose, and the
    # evidence that matters is a concrete file.
    token = re.compile(r"[\w.-]+(?:/[\w.-]+)+(?:\.py|\.md)(?:::[\w\[\]./-]+)?")

    checked = 0
    for key, registration in _STRATEGY_REGISTRY.items():
        contract = registration.signal_program_contract
        if contract is None:
            continue
        provenance = contract.numerical_provenance
        # ``reference`` deliberately excluded: it names external sources
        # (LEAN C# revisions, a TradingView Pine script, a validation PDF)
        # that are not required to exist inside this repo.
        evidence = f"{provenance.validated_against}; {provenance.canonical_implementation}"
        for raw in token.findall(evidence):
            file_part, _, node_part = raw.partition("::")
            resolved = _resolve(file_part)
            assert resolved is not None, (
                f"'{key}' validated_against names '{file_part}', which does not "
                f"exist under {service_root} or {repo_root}. Update the field to "
                "name the evidence that replaced it."
            )
            checked += 1
            if not node_part:
                continue
            function_name = node_part.split("[", 1)[0]
            module = ast.parse(resolved.read_text(encoding="utf-8"))
            defined = {
                node.name
                for node in ast.walk(module)
                if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef)
            }
            assert function_name in defined, (
                f"'{key}' validated_against names '{raw}', but "
                f"'{function_name}' is not defined in {resolved}. A renamed or "
                "deleted test or class leaves this receipt pointing at nothing."
            )

    assert checked, "expected at least one sealed contract to name its evidence"


def test_every_registration_constructs_its_params_with_a_symbol() -> None:
    """Every registered parameter schema builds with no arguments and names a symbol.

    Issue #1736 moved ``symbol`` onto ``StrategyParamsBase`` as a required,
    un-defaulted field, so a subclass that forgets to supply its own default
    stops being constructible. That is the intended failure -- a params model
    with no symbol has nothing for ``alpaca_paper_strategy_default_symbol`` or
    ``paper_deploy_service`` to read -- but it is only a *loud* failure if
    something exercises every registration. Before this test only ``spy_orb``
    was covered (``tests/test_engine_strategies_endpoint.py``), so a new
    registration could have reached Engine Lab or a deploy form before anyone
    found out.
    """
    for key, reg in _STRATEGY_REGISTRY.items():
        params = reg.param_schema()
        assert isinstance(params.symbol, str) and params.symbol, (
            f"'{key}' registered a parameter schema whose default symbol is empty; "
            "every strategy names the signal stream it reads."
        )

