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

from app.engine.strategy.registry import _STRATEGY_REGISTRY


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


def test_ema_crossover_derivatives_do_not_inherit_the_canonical_contract() -> None:
    """Pin the actual fix: both known ``dataclasses.replace`` derivatives of the
    canonical EMA Signal Program registration are explicitly unsealed, not
    accidentally sealed with someone else's identity."""
    canonical = _STRATEGY_REGISTRY["ema_crossover_signal"]
    assert canonical.signal_program_contract is not None
    assert canonical.signal_program_factory is not None

    for key in ("ema_crossover_2_bps", "spy_ema_crossover"):
        reg = _STRATEGY_REGISTRY[key]
        assert reg.signal_program_contract is None, f"'{key}' must not carry a signal_program_contract"
        assert reg.signal_program_factory is None, f"'{key}' must not carry a signal_program_factory"
