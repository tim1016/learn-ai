# BFL-2008-MICRO-001 - Bouchaud-Farmer-Lillo 2008 Paper-Reported Benchmarks

Generated: 2026-05-14

Reference: Jean-Philippe Bouchaud, J. Doyne Farmer, Fabrizio Lillo, "How markets slowly digest changes in supply and demand", arXiv:0809.0822v1.

Vendored source: `references/arxiv-0809.0822v1/source/arXiv-0809.0822v1.tar.gz`

Oracle: paper-reported benchmark values extracted from `handbook20.tex`.

Canonical implementation: planned; see `docs/design/bouchaud-farmer-lillo-2008-microstructure-implementation-design.md`.

Validated against: NONE - pending implementation. This fixture is a literature benchmark inventory, not a raw-market-data equivalence fixture.

## Purpose

The paper reports several empirical validation studies using LSE, PSE, NYSE, and Spanish Stock Exchange datasets. The arXiv source does not include those raw datasets. This fixture preserves the reported values so implementation agents have stable benchmark targets and do not rely on memory.

## Files

- `input.json` - fixture metadata, source archive path, extraction scope, and planned implementation targets.
- `output.json` - paper-reported benchmark values.

## Tolerance

No numerical implementation is validated by this fixture yet. Future parser/smoke tests should compare JSON values exactly. Future formula-level implementations must use separate hand-computed or synthetic fixtures with explicit tolerances.

## Limitations

- Not a strict empirical equivalence fixture.
- Not regenerated from raw market data.
- Must not be used to claim reproduction of the authors' empirical figures.

## Justification

Initial design fixture for the BFL 2008 microstructure implementation plan.
