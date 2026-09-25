"""Safely reproduce disk qualification accepting different imported code.

Run only from a disposable review clone, in a fresh guarded Python process.
The isolated clone's one source file is restored before qualification runs.
No broker or service is constructed; the existing admission fixture is pure.
"""
from __future__ import annotations

import importlib
import logging
import sys
from decimal import Decimal
from pathlib import Path

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
root = Path.cwd().resolve()
if not str(root).startswith('/Users/inkant/codex-review-20260924/'):
    raise RuntimeError('Reproduction requires an isolated review clone')
module_name = 'app.engine.strategy.algorithms.ema_crossover_signal'
if module_name in sys.modules:
    raise RuntimeError('Reproduction requires a fresh interpreter')
source = root/'app/engine/strategy/algorithms/ema_crossover_signal.py'
qualified_bytes = source.read_bytes()
old_condition = b'if ema_fast - ema_slow < self._gap:'
different_condition = b'if ema_fast - ema_slow < self._gap * Decimal(2):'
assert qualified_bytes.count(old_condition) == 1
try:
    source.write_bytes(qualified_bytes.replace(old_condition, different_condition))
    # Model a process that imports revision A, then sees a host update to B.
    # This is an ordinary import, not monkeypatching any admission function.
    algorithm_module = importlib.import_module(module_name)
finally:
    source.write_bytes(qualified_bytes)

fixtures = importlib.import_module('tests.services.test_signal_program_admission')
admission = importlib.import_module('app.services.signal_program_admission')
binding = fixtures._sealed_binding()
proof = admission.prove_running_program_build(binding, verified_at_ms=fixtures._NOW)
algorithm = algorithm_module.EmaCrossoverSignalAlgorithm()
observed = algorithm._gap_is_sufficient(Decimal('100.30'), Decimal('100'))
expected_from_qualified_source = True  # $0.30 satisfies the qualified $0.20 floor.
assert source.read_bytes() == qualified_bytes
logger.info('Loaded-code observation: proof=%s actual_gap_pass=%s qualified_gap_pass=%s',
            proof.state, observed, expected_from_qualified_source)
assert proof.state != 'PROVEN' or observed == expected_from_qualified_source, (
    'PROVEN was returned for qualified on-disk bytes while the loaded strategy '
    'rejects a gap those bytes accept'
)
