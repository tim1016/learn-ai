# Cross-stack numerical and temporal transport

Research ticket: [#2435](https://github.com/tim1016/learn-ai/issues/2435). Parent map: [#2413](https://github.com/tim1016/learn-ai/issues/2413).

Baseline: `10b5f31b529c8507bd19bb24015f3d85fa9aba43`. Review date: 2026-09-24. Branch: `research/codex-2435-transport`.

## Conclusion

**No new Critical, High, or Medium finding established in this bounded pass.** The representative active consumers preserve producer-authored monetary/statistical values and nullable states. Millisecond timestamps survive the sampled adapters; seconds conversions occur at chart-library inputs. This is evidence about the examined mappings, not a claim that every endpoint or historical payload is correct.

The execution checks passed: **56 existing Python tests and eight synthetic frontend checks**. The frontend checks execute unchanged pure functions from the baseline source, without bootstrapping Angular. No .NET runtime, real browser, broker, vendor feed, service, or database was exercised.

## Scope and route authority

The active Strategy Tools routes are Spec Strategy, Strategy Validation, Strategy Lab, Grid Search, and Walk-Forward; the active Walk-Forward component is `walk-forward-study`, not the similarly named Research component. [Route definitions, lines 120 and 210–238](https://github.com/tim1016/learn-ai/blob/10b5f31b529c8507bd19bb24015f3d85fa9aba43/Frontend/src/app/app.routes.ts#L210).

The frontend development proxy sends `/graphql` and `/api/jobs` to .NET, and the remaining `/api` surface to the data plane. This matters because most sampled scoped numerical responses do not undergo a .NET DTO conversion at all. The proxy was read as source and was not executed. [Proxy routing, lines 156–172](https://github.com/tim1016/learn-ai/blob/10b5f31b529c8507bd19bb24015f3d85fa9aba43/Frontend/proxy.conf.js#L156).

| Active surface | Boundary and consumer examined | Outcome |
| --- | --- | --- |
| Stocks chart | Python chart response → candle/indicator inputs | OHLC and indicator values pass through; chart timestamps divide milliseconds by 1000. Volume's `?? 0` is a plotting fallback, not a return calculation. [Chart adapter, lines 620–644](https://github.com/tim1016/learn-ai/blob/10b5f31b529c8507bd19bb24015f3d85fa9aba43/Frontend/src/app/components/data-lab/data-lab-chart/data-lab-chart.component.ts#L620). |
| Stocks Returns | Python distribution DTO → camelCase study model | Null volatility, missing returns, edge-bin bounds, coverage timestamps, and zero-valued bin indices remain distinct. Synthetic execution verified the actual adapter. [Mapping, lines 107–173](https://github.com/tim1016/learn-ai/blob/10b5f31b529c8507bd19bb24015f3d85fa9aba43/Frontend/src/app/components/data-lab/returns-distribution/returns-distribution.service.ts#L107). |
| Stocks lake | Observatory's initial trading dates and unavailable resource states | Initial dates use the shared ET date formatter; first/last session state is nullable. Only this boundary was sampled; catalog integrity was not re-audited. [Source, lines 23–47](https://github.com/tim1016/learn-ai/blob/10b5f31b529c8507bd19bb24015f3d85fa9aba43/Frontend/src/app/components/data-lake-observatory/data-lake-observatory.component.ts#L23). |
| Accounts | Broker contracts → account, position, activity and portfolio-history views | Prices/rates and activity amounts retain unavailable states; the portfolio chart plots supplied equity rather than calculating P&L. [Positions, lines 81–108](https://github.com/tim1016/learn-ai/blob/10b5f31b529c8507bd19bb24015f3d85fa9aba43/Frontend/src/app/components/brokers/alpaca-desk/alpaca-positions-table.component.html#L81), [activity, lines 69–82](https://github.com/tim1016/learn-ai/blob/10b5f31b529c8507bd19bb24015f3d85fa9aba43/Frontend/src/app/components/brokers/alpaca-desk/alpaca-trader-activity-table.component.html#L69), [equity chart, lines 59–66](https://github.com/tim1016/learn-ai/blob/10b5f31b529c8507bd19bb24015f3d85fa9aba43/Frontend/src/app/components/brokers/alpaca-desk/alpaca-portfolio-history-chart.component.ts#L59). |
| Strategy Validation | Direct typed API reads → producer proof states and diagnostics | No numerical transport conversion found in the sampled service. Missing proof is rendered as incomplete evidence; this review does not revalidate the producer's admission decision. [Service, lines 14–43](https://github.com/tim1016/learn-ai/blob/10b5f31b529c8507bd19bb24015f3d85fa9aba43/Frontend/src/app/services/strategy-validation.service.ts#L14), [consumer, lines 82–95](https://github.com/tim1016/learn-ai/blob/10b5f31b529c8507bd19bb24015f3d85fa9aba43/Frontend/src/app/components/strategy-validation/strategy-validation.component.html#L82). |
| Spec Strategy | Generated Python contract → result and trade table | Optional configuration is sent only when specified; fees/P&L are displayed from response fields and fractional rates are scaled only for display. [Service, lines 65–94](https://github.com/tim1016/learn-ai/blob/10b5f31b529c8507bd19bb24015f3d85fa9aba43/Frontend/src/app/services/spec-strategy.service.ts#L65), [results, lines 492–524](https://github.com/tim1016/learn-ai/blob/10b5f31b529c8507bd19bb24015f3d85fa9aba43/Frontend/src/app/components/spec-strategy-runner/spec-strategy-runner.component.html#L492). |
| Strategy Lab | Python persisted response → report and history adapters | Fees, equity, P&L, ratios and trade P&L pass through. Nullable Sharpe/Sortino/profit factor remain nullable; headline formatting renders absent/nonfinite numbers as a dash. [Report, lines 107–136 and 273–286](https://github.com/tim1016/learn-ai/blob/10b5f31b529c8507bd19bb24015f3d85fa9aba43/Frontend/src/app/components/strategy-lab/strategy-lab-run-report.service.ts#L107), [formatting, lines 32–55](https://github.com/tim1016/learn-ai/blob/10b5f31b529c8507bd19bb24015f3d85fa9aba43/Frontend/src/app/components/strategy-lab/results-summary/results-summary.component.ts#L32). |
| Grid Search | Direct detail/cell reads → result table | Nullable metrics render as unavailable. Fractional returns, drawdown, and win rate use percent formatting; selection is not recomputed in this table. [Client, lines 92–104](https://github.com/tim1016/learn-ai/blob/10b5f31b529c8507bd19bb24015f3d85fa9aba43/Frontend/src/app/components/grid-search/grid-search.service.ts#L92), [table, lines 77–89](https://github.com/tim1016/learn-ai/blob/10b5f31b529c8507bd19bb24015f3d85fa9aba43/Frontend/src/app/components/grid-search/grid-search-result.component.html#L77). |
| Walk-Forward | Direct study read → verdict/fold table | Median Sharpe and retention nulls render as unavailable. Half-open window ends subtract one millisecond only to label the final included trading date. [Client, lines 57–59](https://github.com/tim1016/learn-ai/blob/10b5f31b529c8507bd19bb24015f3d85fa9aba43/Frontend/src/app/components/walk-forward-study/walk-forward-study.service.ts#L57), [table, lines 76–85](https://github.com/tim1016/learn-ai/blob/10b5f31b529c8507bd19bb24015f3d85fa9aba43/Frontend/src/app/components/walk-forward-study/walk-forward-study-result.component.html#L76). |

## Satisfied controls and evidence strength

**Proven by source: .NET job JSON has no monetary/statistical rewrite.** `GetJobResultAsync` returns the stored JSON string as `application/json`, with a distinct not-found outcome. SSE forwards the event's JSON body as well. This avoids decimal-to-double DTO remapping on that path. This is static evidence, not an executed .NET integration test. [Jobs API, lines 252–284](https://github.com/tim1016/learn-ai/blob/10b5f31b529c8507bd19bb24015f3d85fa9aba43/Backend/Jobs/JobsApi.cs#L252).

**Reproduced: financial display adapters preserve supplied values.** A synthetic trade deliberately had positive displayed price movement but negative producer P&L. The real `toEngineTrade` kept `pnl_pts=-0.5`, `pnl_pct=-0.005`, and the negative outcome instead of recalculating from prices. The history adapter retained unknown commission/brokerage and a fractional negative P&L. Returns preserved both null and genuine zero inputs. [Executable review checks](https://github.com/tim1016/learn-ai/blob/research/codex-2435-transport/review/codex_2435_transport.cjs).

**Reproduced: timestamp meaning is explicit in the shared helper.** Tests covered spring-forward, both distinct fall-back instants and their ISO offsets, UTC date markers, viewer-local instants, winter/summer ET run-date anchors, and chart-only millisecond-to-second conversion. All integer milliseconds through the documented maximum `253402300799999` are safely representable in JavaScript; representative JSON round trips retained the exact integer. The .NET session input bound uses that maximum. [Shared helper, lines 85–133](https://github.com/tim1016/learn-ai/blob/10b5f31b529c8507bd19bb24015f3d85fa9aba43/Frontend/src/app/shared/timestamp/timestamp-display.ts#L85), [.NET bound, lines 16–42](https://github.com/tim1016/learn-ai/blob/10b5f31b529c8507bd19bb24015f3d85fa9aba43/Backend/GraphQL/DataLabMutation.cs#L16).

**Proven by source, partly covered by existing tests: producer contracts preserve field identity.** Backtest responses explicitly alias `totalPnL`, `pnL`, timestamp columns and nullable ratio fields; broker positions distinguish nullable current price/rate from required monetary values. Portfolio history validates aligned series lengths and strictly increasing timestamps. [Backtest schema, lines 37–120](https://github.com/tim1016/learn-ai/blob/10b5f31b529c8507bd19bb24015f3d85fa9aba43/PythonDataService/app/schemas/backtest_runs.py#L37), [broker models, lines 173–187 and 276–301](https://github.com/tim1016/learn-ai/blob/10b5f31b529c8507bd19bb24015f3d85fa9aba43/PythonDataService/app/broker/contract/models.py#L276).

No ADR is challenged. The broker model explicitly describes floats as “broker-reported figures for a read-only display surface” and says verbatim decimal strings are retained separately. This review did not equate ordinary display floats with evidence of a material accounting error, nor prove arbitrary-precision transport. [Documented rationale, lines 14–18](https://github.com/tim1016/learn-ai/blob/10b5f31b529c8507bd19bb24015f3d85fa9aba43/PythonDataService/app/broker/contract/models.py#L14).

## Checks and safety

All tests used the isolated clone. The Python launcher rejected network/DNS/binds, child processes, protected files, and writes outside the review area; environment variables were cleared, with only a synthetic configuration key supplied. Root application test fixtures and automatic plugins were disabled.

From `PythonDataService`, with the guarded launcher and `--noconftest`:

```text
pytest -q --noconftest tests/research/backtest_runs/test_records.py tests/unit/routers/test_engine_trade_ms_timestamp.py tests/utils/test_timestamps.py
33 passed
pytest -q --noconftest tests/broker/contract/test_models.py
23 passed
```

Both runs emitted only the expected `asyncio_mode` configuration warning because automatic plugin loading was disabled. No asynchronous tests were selected.

From the clone root, the committed synthetic check used Node 26.4.0 with an empty environment apart from PATH, isolated HOME, and the read-only TypeScript compiler path:

```text
node --permission --allow-fs-read=<clone> --allow-fs-read=<host>/Frontend/node_modules/typescript/lib/typescript.js review/codex_2435_transport.cjs
8 checks passed
```

Node permissions granted no network, writes, or child processes. Pure source ran in a VM without application imports, `require`, `process`, `fetch`, or filesystem access. AST selection retained complete production function declarations without rewriting their bodies. This is deliberately narrower than Angular component execution.

## Limitations, non-findings and follow-up triggers

- .NET is unavailable on the host, so its serializers/resolvers and existing .NET timestamp tests were inspected rather than executed. No database-backed or browser tests ran. The dev proxy routing is source evidence, not evidence of deployed configuration.
- Legacy Data Lab saved-session strings and numeric fields coexist. The current UI still saves/restores its date strings; the backend documents UTC-midnight numeric derivation as interim. No active consumer consequence from conflicting values was established here. [Backend rationale, lines 47–61](https://github.com/tim1016/learn-ai/blob/10b5f31b529c8507bd19bb24015f3d85fa9aba43/Backend/GraphQL/DataLabMutation.cs#L47), [UI adapter, lines 300–334](https://github.com/tim1016/learn-ai/blob/10b5f31b529c8507bd19bb24015f3d85fa9aba43/Frontend/src/app/services/data-lab-session.service.ts#L300).
- Spec Strategy's trade formatter uses fixed New York time without an inline zone marker. This is a bounded display inconsistency; no mutation of persisted timestamps or downstream execution window was established. It is not counted as a material transport finding. [Formatter, lines 713–722](https://github.com/tim1016/learn-ai/blob/10b5f31b529c8507bd19bb24015f3d85fa9aba43/Frontend/src/app/components/spec-strategy-runner/spec-strategy-runner.component.ts#L713).
- Python numerical correctness, known receipt/admission findings, chart/export range behavior, and economic-record completeness remain with their owning investigations; this ticket adds no duplicate finding. Research-only and Options-only consumers are excluded even where their service names resemble active Strategy Tools.
- Confidence is high in the executed pure mappings, moderate in the statically traced request-to-view paths. A new nullable contract field, changed unit, reused .NET numerical resolver, or concrete stale/historical payload that a current consumer misinterprets would justify a focused follow-up. No unresolved owner decision blocks this result.
