"""Independent synthetic invariants for CSV comparison evidence coverage."""
from __future__ import annotations

from app.services.validation_service import generate_validation_report


def test_full_unique_aligned_comparison_is_excellent() -> None:
    data = b'unix_ts,ema_5\n1700000000000,42\n1700000060000,43\n'
    report = generate_validation_report(data, data, 'SYNTHETIC')
    assert 'Excellent' in report
    assert '| Match rate | 100.0% |' in report


def test_known_aligned_disagreement_is_reported() -> None:
    ours = b'unix_ts,ema_5\n1700000000000,42\n1700000060000,43\n'
    theirs = b'unix_ts,ema_5\n1700000000000,84\n1700000060000,86\n'
    report = generate_validation_report(ours, theirs, 'SYNTHETIC')
    assert 'Significant divergence' in report


def test_missing_indicator_coverage_prevents_unqualified_excellent_grade() -> None:
    ours = ['unix_ts,ema_5']
    theirs = ['unix_ts,ema_5']
    for index in range(1000):
        timestamp = 1700000000000 + 60000 * index
        ours.append(f'{timestamp},' + ('42' if index == 999 else ''))
        theirs.append(f'{timestamp},42')
    report = generate_validation_report('\n'.join(ours).encode(), '\n'.join(theirs).encode(), 'SYNTHETIC')
    assert '| Matched (aligned) rows | 1,000 |' in report
    assert '| **Total compared** | **1** | |' in report
    assert 'Excellent' not in report


def test_duplicate_timestamps_cannot_inflate_evidence_coverage() -> None:
    data = b'unix_ts,ema_5\n1700000000000,42\n1700000000000,42\n'
    report = generate_validation_report(data, data, 'SYNTHETIC')
    assert '| Match rate | 200.0% |' not in report


def test_disjoint_clock_columns_cannot_prove_timestamp_equivalence() -> None:
    ours = b'unix_ts,ema_5\n1700000000000,42\n1700000060000,43\n'
    theirs = b'time,ema_5\n1800000000000,42\n1800000060000,43\n'
    report = generate_validation_report(ours, theirs, 'SYNTHETIC')
    assert '**Alignment:** positional' in report
    assert 'Excellent' not in report
