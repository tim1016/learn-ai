# Kill list — Backend dead code and Backend.Tests (#2710)

Part of map #2700. Read at **`87b8e261021ec673c2e7c80448c0973bccd45378`** (`origin/master`, same SHA the map was charted at).

## How this was built

- **GraphQL reachability.** Every GraphQL template literal in `Frontend/src` (non-spec `.ts`) was parsed and its root selections extracted, then compared with the 75 root fields in `contracts/graphql/backend.schema.graphql`. For each root that *is* selected, I checked that the Frontend service method holding the document has a non-spec caller (`.method(` anywhere in `Frontend/src` `.ts`/`.html`). A root field is dead when no document selects it, or when the only document sits in a Frontend method nothing calls.
- **Symbol reachability.** Every declared C# member under `Backend/` (migrations excluded) was counted for whole-word references across `Backend/` and `Backend.Tests/`. Every candidate was then confirmed with a targeted `grep` across `Frontend/`, `PythonDataService/`, `scripts/`, the compose files, `deploy/` and `.github/`.
- **Python never touches the Backend's tables directly**, apart from `"DataLakeArtifacts"`. A grep for the other EF table names in `PythonDataService/app` and `scripts` finds nothing, so an EF entity with no Backend writer and no live reader is dead.
- **Tests.** Every `[Fact]`/`[Theory]` in `Backend.Tests` (≈150) was read for what it actually asserts.
- No static-analysis tool was installed. There is no `dotnet` on the host, so nothing was compiled.

"No Frontend selection" below means the parsed-document check found no selection. "Dead wrapper" means the field is selected only inside a Frontend method that has no caller. That method is a pointer to #2709 (see Pointers).

## A. Dead code — GraphQL root fields

Each row covers the resolver plus anything used *only* by it (result/input types in the same file). Section B adds the service, DTO and entity cascade.

| # | Path | Kind | Evidence |
|---|---|---|---|
| A1 | `Backend/GraphQL/Query.cs:28` `tickers` (`GetTickers`) | GraphQL root | Selected only by `Frontend/src/app/services/ticker.service.ts:53` `getTickers`. The whole `TickerService` is imported only by its own spec. |
| A2 | `Backend/GraphQL/Query.cs:38` `stockAggregates` | GraphQL root | No Frontend selection. |
| A3 | `Backend/GraphQL/Query.cs:41` `stockAggregateStats` + `StockAggregateStatsType` (`Query.cs:1387`) | GraphQL root | Selected only by `ticker.service.ts:68` `getAggregateStats` (no caller). |
| A4 | `Backend/GraphQL/Query.cs:86` `trades` | GraphQL root | No Frontend selection (the word `trades` appears only as a nested field). |
| A5 | `Backend/GraphQL/Query.cs:95` `quotes` | GraphQL root | No Frontend selection. |
| A6 | `Backend/GraphQL/Query.cs:104` `technicalIndicators` | GraphQL root | No Frontend selection. |
| A7 | `Backend/GraphQL/Query.cs:112` `tickerBySymbol` | GraphQL root | No Frontend selection. |
| A8 | `Backend/GraphQL/Query.cs:115` `getFetchProgress` + `FetchProgressInfo` (`Types/SmartAggregatesResult.cs:43`) | GraphQL root | Dead wrapper: `market-data.service.ts:529` `getFetchProgress`. |
| A9 | `Backend/GraphQL/Query.cs:232` `calculateIndicators` + `IndicatorConfigInput` (`Query.cs:1505`) + `Types/CalculateIndicatorsResult.cs` | GraphQL root | Dead wrapper: `market-data.service.ts:620`. |
| A10 | `Backend/GraphQL/Query.cs:336` `generateIndicatorTable` + `Types/IndicatorTableResult.cs` | GraphQL root | No Frontend selection. |
| A11 | `Backend/GraphQL/Query.cs:409` `availableIndicators` + `Types/AvailableIndicatorsResult.cs` | GraphQL root | No Frontend selection. |
| A12 | `Backend/GraphQL/Query.cs:451` `checkCachedRanges` + `DateRangeInput`/`CachedRangeResult` (`Query.cs:1492-1503`) | GraphQL root | Dead wrapper: `market-data.service.ts:540`. |
| A13 | `Backend/GraphQL/Query.cs:497` `getOptionsContracts` + `OptionsContractsResult`/`OptionsContractResult` (`Query.cs:1466-1482`) | GraphQL root | Dead wrapper: `market-data.service.ts:561`. |
| A14 | `Backend/GraphQL/Query.cs:724` `getStockSnapshots` + `StockSnapshotsResult` (`Query.cs:1549`) | GraphQL root | Dead wrapper: `market-data.service.ts:678`. `MapTickerSnapshot` stays because `getStockSnapshot` is live. |
| A15 | `Backend/GraphQL/Query.cs:755` `getMarketMovers` + `MarketMoversResult` (`Query.cs:1557`) | GraphQL root | Dead wrapper: `market-data.service.ts:694`. |
| A16 | `Backend/GraphQL/Query.cs:785` `getUnifiedSnapshot` + `Unified*Result` (`Query.cs:1565-1593`) | GraphQL root | Dead wrapper: `market-data.service.ts:710`. |
| A17 | `Backend/GraphQL/Query.cs:1127` `getResearchExperiment` | GraphQL root | Dead wrapper: `research.service.ts:1200` `getExperiment` (no caller; the history list uses `getExperiments`). |
| A18 | `Backend/GraphQL/Query.cs:1201` `quantlibStatus` + `QuantLibStatusResult` (`Query.cs:1743`) | GraphQL root | Dead wrapper: `quantlib.service.ts:127` `checkStatus`. Only `priceOption` is called (`broker-options-chain.component.ts:387`). |
| A19 | `Backend/GraphQL/Query.cs:1272` `quantlibStrategy` + `QuantLibLegResultGql`/`QuantLibStrategyResult` (`Query.cs:1765-1790`) | GraphQL root | Dead wrapper: `quantlib.service.ts:172` `priceStrategy`. |
| A20 | `Backend/GraphQL/Mutation.cs:27` `fetchStockAggregates` + `Types/FetchAggregatesResult.cs` | GraphQL root | No Frontend selection. `FetchAndStoreAggregatesAsync` stays because the live `GetOrFetch` path uses it (`MarketDataService.cs:266`). |
| A21 | `Backend/GraphQL/Mutation.cs:64` `sanitizeMarketData` + `Types/SanitizeResult.cs` | GraphQL root | No Frontend selection. |
| A22 | `Backend/GraphQL/Mutation.cs:99` `runFeatureResearch` | GraphQL root | Dead wrapper: `research.service.ts:1135`. The feature runner now starts the job `feature_research` through `/api/jobs` (`feature-runner.component.ts:375`). |
| A23 | `Backend/GraphQL/Mutation.cs:134` `runSignalEngine` | GraphQL root | Dead wrapper: `research.service.ts:1174`. The signal runner calls `jobsService.startJob('signal_engine', …)` (`signal-runner.component.ts:323`). |
| A24 | `Backend/GraphQL/Mutation.cs:173` `runOptionsFeatureResearch` | GraphQL root | Dead wrapper: `research.service.ts:1248`. |
| A25 | `Backend/GraphQL/Mutation.cs:207` `runBatchOptionsResearch` + `BatchResearchResultType`/`TickerBatchResultType` (`Mutation.cs:386-416`) | GraphQL root | Dead wrapper: `research.service.ts:1270`. |
| A26 | `Backend/GraphQL/Mutation.cs:273` `runRuleBasedBacktest` + `RuleBasedBacktestResultType`/`RuleBasedTradeType` (`Mutation.cs:417-503`) | GraphQL root | Dead wrapper: `market-data.service.ts:774` (no `.runRuleBasedBacktest(` caller anywhere). |
| A27 | `Backend/GraphQL/PortfolioQuery.cs:29` `getAccount` | GraphQL root | No Frontend selection (the portfolio UI uses `getAccounts`). `brokers.getAccount` is an unrelated REST call. |
| A28 | `Backend/GraphQL/PortfolioQuery.cs:45` `getPosition` | GraphQL root | No Frontend selection. |
| A29 | `Backend/GraphQL/PortfolioQuery.cs:55` `getPortfolioTrades` | GraphQL root | No Frontend selection. |
| A30 | `Backend/GraphQL/PortfolioQuery.cs:65` `getPositionLots` | GraphQL root | No Frontend selection. |
| A31 | `Backend/GraphQL/PortfolioQuery.cs:79` `getPortfolioValuation` | GraphQL root | Dead wrapper: `portfolio.service.ts:119` `getValuation`. `ComputeValuationAsync` stays because `SnapshotService.cs:25` uses it. |
| A32 | `Backend/GraphQL/PortfolioQuery.cs:90` `getPortfolioSnapshots` | GraphQL root | No Frontend selection (the equity chart uses `getEquityCurve`). |
| A33 | `Backend/GraphQL/PortfolioQuery.cs:144` `getPortfolioVega` | GraphQL root | No Frontend selection. `ComputePortfolioVegaAsync` stays because the MaxVega rule uses it (`PortfolioRiskService.cs:244`). |
| A34 | `Backend/GraphQL/PortfolioMutation.cs:34` `submitOrder` | GraphQL root | No Frontend, Python or script caller. This is the Backend paper ledger, not a broker order path; the portfolio UI books trades through `recordTrade`. |
| A35 | `Backend/GraphQL/PortfolioMutation.cs:63` `cancelOrder` + `OrderResult` (`PortfolioMutation.cs:328`) | GraphQL root | No caller (as A34). |
| A36 | `Backend/GraphQL/PortfolioMutation.cs:79` `fillOrder` | GraphQL root | No caller (as A34). `TradeResult` stays because `recordTrade` uses it. |
| A37 | `Backend/GraphQL/DataLabMutation.cs:204` `updateDataLabChartSnapshot` | GraphQL root | Dead wrapper: `data-lab-session.service.ts:267` `updateChartSnapshot`. |

## B. Dead code — services, DTOs, entities, config

| # | Path | Kind | Evidence |
|---|---|---|---|
| B1 | `Backend/Services/Implementation/TechnicalAnalysisService.cs`, `Services/Interfaces/ITechnicalAnalysisService.cs`, `Program.cs:75-82` (HttpClient registration) | service (whole) | Its three methods are called only by A9/A10/A11 (`Query.cs:294,378,416`). |
| B2 | `Backend/Models/DTOs/IndicatorModels.cs` | DTO file (whole) | `OhlcvBarDto`/`IndicatorConfigDto`/`Indicator*Dto` are used only by B1, A9 and the dead research requests (B6–B8). |
| B3 | `Backend/Services/Implementation/SanitizationService.cs`, `Services/Interfaces/ISanitizationService.cs`, `Program.cs:65-72`, `Models/DTOs/SanitizeModels.cs` | service (whole) | Only caller is A21 (`Mutation.cs:71`). |
| B4 | `ResearchService.RunFeatureResearchAsync` (`ResearchService.cs:35`), `RunSignalEngineAsync` (`:176`), `PersistSignalExperimentAsync` (`:301`), `RunOptionsFeatureResearchAsync` (`:345`), `RunBatchOptionsResearchAsync` (`:472`), `PersistIvDataAsync` (`:506`), plus their `IResearchService` members | service methods | Callers are only A22–A25 (`Mutation.cs:116,154,189,223`). |
| B5 | `ResearchService.GetExperimentAsync` (`ResearchService.cs:144`) | service method | Only caller is A17 (`Query.cs:1132`). |
| B6 | `Backend/Models/DTOs/ResearchModels.cs`: everything except `ResearchExperimentDto` | DTOs | Used only by B4 and `ResearchResultMapper`. `ResearchExperimentDto` stays for the live `getResearchExperiments`. |
| B7 | `Backend/Models/DTOs/BatchResearchModels.cs` | DTO file (whole) | Every class is used only by `ResearchService.cs:364-498` (B4). |
| B8 | `Backend/Models/DTOs/SignalModels.cs` `RunSignalEngineRequest` | DTO | Only use is `ResearchService.cs:218` (B4). `SignalEngineReportDto` and its tree stay for the live `getSignalExperimentReport`. |
| B9 | `Backend/GraphQL/Types/ResearchResultMapper.cs`; `Types/ResearchResult.cs:5-281` (`ResearchResultType` and its 17 nested types) | GraphQL types | Mapper is called only at `Mutation.cs:119,192` (A22/A24). None of the nested types is referenced outside these two files. `ResearchExperimentType` (`:282`) stays. |
| B10 | `PolygonService.FetchOptionsContractsAsync` (`PolygonService.cs:514`), `IPolygonService` member, `OptionsContractsResponse`/`OptionsContractDto` (`PolygonResponses/OptionsContractsResponse.cs:3,19`) | service method + DTOs | Only caller is A13 (`Query.cs:517`). `OptionsExpirationsResponse` stays. |
| B11 | `PolygonService.FetchMarketMoversAsync` (`:207`), `FetchUnifiedSnapshotAsync` (`:237`); `MarketMoversResponse`, `UnifiedSnapshot*` (`PolygonResponses/StockSnapshotResponses.cs:45-81`) | service methods + DTOs | Only callers are A15/A16 (`Query.cs:765,797`). `FetchStockSnapshotsAsync` stays because valuation and risk use it. |
| B12 | `PolygonService.QuantLibStatusAsync` (`:770`), `QuantLibStrategyAsync` (`:719`); `QuantLibStatusResponse`, `QuantLibStrategyResponse`, `QuantLibLegResult` (`PolygonResponses/QuantLibResponse.cs:24-63`) | service methods + DTOs | Only callers are A18/A19 (`Query.cs:1208,1286`). |
| B13 | `PolygonService.FetchTradesAsync` (`PolygonService.cs:622`, `IPolygonService.cs:55`), `PolygonResponses/TradeResponse.cs`, `PolygonResponses/TradeData.cs`, `GraphQL/Types/FetchTradesResult.cs` | service method + DTOs | No caller anywhere in `Backend/`. Only `PolygonServiceTests.cs:112,131` call it. `FetchTradesResult` has 0 references. |
| B14 | `MarketDataService._progressTracker`/`GetProgress` (`MarketDataService.cs:17-23`), the progress writes in the windowed fetch (`MarketDataService.cs:242-314`), `FetchProgress` (`Models/DTOs/GapDetectionModels.cs:26`) | write-only state | The only reader is A8 (`Query.cs:118`). Once A8 goes, the tracker is written and never read. |
| B15 | `PortfolioService.SubmitOrderAsync` (`PortfolioService.cs:43`), `CancelOrderAsync` (`:72`), `FillOrderAsync` (`:87`) + `IPortfolioService` members | service methods | Only callers are A34–A36 (`PortfolioMutation.cs:48,70,90`). The `Order` entity stays because `RecordTradeAsync` creates one (`PortfolioService.cs:178`). |
| B16 | `PositionEngine.CalculateRealizedPnL` (`PositionEngine.cs:94`, `IPositionEngine.cs:9`) | method | 0 Backend callers. Only `PositionEngineTests.cs:276` calls it. |
| B17 | `SnapshotService.TakeSnapshotWithPricesAsync` (`SnapshotService.cs:29`, `ISnapshotService.cs:8`) | method | 0 callers anywhere, tests included. |
| B18 | `StockAggregate.IsValid()` (`Models/MarketData/StockAggregate.cs:35`) | method (also exposed as GraphQL `StockAggregate.isValid`) | 0 Backend callers. No Frontend document selects `isValid`. Only `StockAggregateTests.cs` calls it. |
| B19 | EF entity `Trade` (`Models/MarketData/Trade.cs`, `AppDbContext.cs:17,99-110`, `Ticker.Trades` `Ticker.cs:39`) | EF entity | Nothing writes it (no `.Trades.Add` in Backend; no Python/script reference to the table). The only reader is A4. **Migration.** |
| B20 | EF entity `Quote` incl. `GetSpread`/`GetMidPrice` (`Models/MarketData/Quote.cs:29,34`, `AppDbContext.cs:18,112-125`, `Ticker.Quotes` `Ticker.cs:40`) | EF entity | Nothing writes it. The only reader is A5. **Migration.** |
| B21 | EF entity `TechnicalIndicator` (`Models/MarketData/TechnicalIndicator.cs`, `AppDbContext.cs:19,127-139`) | EF entity | Nothing writes it. The only reader is A6. **Migration.** |
| B22 | EF entity `ReferenceData` (`Models/MarketData/ReferenceData.cs`, `AppDbContext.cs:20,141-153`) | EF entity | No reader or writer at all beyond its `DbSet`. **Migration.** |
| B23 | EF entity `OptionsIvSnapshot` (`Models/MarketData/OptionsIvSnapshot.cs`, `AppDbContext.cs:32,186-201`) | EF entity | Read and written only by B4 (`ResearchService.cs:371,541`). Python's IV recorder keeps its own store (`app/services/iv_recorder.py`). **Migration.** |
| B24 | `Backend/Data/PostgresErrors.cs` | file (whole) | `PostgresErrors`/`IsUniqueViolation` have 0 references in Backend or tests. |
| B25 | `UnixMs.ToUtcDateTime` (`Backend/Temporal/UnixMs.cs:14`) | method | 0 references. `FromUtc` stays. |
| B26 | `JobsApi.JsonOpts` (`Backend/Jobs/JobsApi.cs:27-30`) | private field | Declared and never read. |
| B27 | `PolygonServiceOptions.TimeoutSeconds`/`MaxRetries` (`Configuration/PolygonServiceOptions.cs:12-13`) + `appsettings.json:14-15` | config keys | Nothing reads them. The timeouts are hard-coded per client (`Program.cs:59,69,79,94,124`). The `MaxRetries` hit in `DatabaseInitializer.cs:13` is that file's own constant. |
| B28 | `StockTickerSnapshotResult.PrevDay/Min/TodaysChange/TodaysChangePercent/Updated` + `MinuteBarResult` (`Query.cs:1525-1539`) and the matching mapping lines in `MapTickerSnapshot` (`Query.cs:832`) | GraphQL output fields | `getStockSnapshot` is live, but no Frontend document selects these five fields (selected-token scan). `MinuteBarResult` is used only by `Min`. |
| B29 | `PortfolioMetrics.TotalReturn` (`ISnapshotService.cs:34`, set at `SnapshotService.cs:150`), `PortfolioState.TotalRealizedPnL` (`IPortfolioService.cs:49`, set at `PortfolioService.cs:271`) | GraphQL output fields | Never selected by any Frontend document. Minor. |

## C. Dead code's tests (go with section A/B)

| # | Path | Kind | Evidence |
|---|---|---|---|
| C1 | `Backend.Tests/Unit/GraphQL/MutationSanitizeTests.cs` | test file | Tests A21 only. |
| C2 | `Backend.Tests/Unit/GraphQL/MutationTests.cs` | test file | Its 3 tests are all `FetchStockAggregates_*` (A20). |
| C3 | `Backend.Tests/Unit/Services/SanitizationServiceTests.cs` | test file | Tests B3 only. |
| C4 | `Backend.Tests/Unit/Services/TechnicalAnalysisServiceTests.cs` | test file | Tests B1 only. |
| C5 | `Backend.Tests/Unit/Models/StockAggregateTests.cs` | test file | Its 5 tests are all `IsValid_*` (B18). |
| C6 | `QueryTests.cs::CheckCachedRanges_NoTicker_ReturnsAllUncached` (:103), `::CheckCachedRanges_WithCachedData_ReturnsCachedTrue` (:121) | test | Test A12. |
| C7 | `QueryTests.cs::GetOptionsContracts_Success_MapsContracts` (:161), `::GetOptionsContracts_ServiceThrows_ReturnsErrorResult` (:190) | test | Test A13. |
| C8 | `QueryTests.cs::CalculateIndicators_NoTicker_ReturnsFailure` (:284), `::CalculateIndicators_NoAggregates_ReturnsFailure` (:300), `::CalculateIndicators_WithData_ReturnsIndicators` (:319), `::CalculateIndicators_ServiceThrows_ReturnsErrorResult` (:363) | test | Test A9. |
| C9 | `PolygonServiceTests.cs::FetchTradesAsync_Success_ReturnsResponse` (:112), `::FetchTradesAsync_PythonReturnsError_Throws` (:131) | test | Test B13. |
| C10 | `PolygonServiceTests.cs::FetchOptionsContractsAsync_Success_ReturnsContracts` (:193) | test | Tests B10. |
| C11 | `ResearchServiceTests.cs::RunFeatureResearchAsync_*` (:101, :138, :159) | test | Test B4. |
| C12 | `ResearchServiceTests.cs::RunSignalEngineAsync_*` (:359, :396, :417, :436) | test | Test B4. |
| C13 | `ResearchServiceTests.cs::GetExperimentAsync_NotFound_ReturnsNull` (:262), `::GetExperimentAsync_Found_ReturnsExperiment` (:277) | test | Test B5. |
| C14 | `PortfolioServiceTests.cs::SubmitOrder_CreatesOrderWithPendingStatus` (:52), `::CancelOrder_SetsCancelledStatus` (:67), `::CancelOrder_AlreadyFilled_Throws` (:81), `::FillOrder_Buy_DeductsCashCorrectly` (:100), `::FillOrder_Sell_AddsCashCorrectly` (:117) | test | Test B15. These touch orders and cash, so the money-path rule was considered. They test the Backend paper ledger, which no caller reaches, and the map's rule is that dead beats sacred. |
| C15 | `PositionEngineTests.cs::CalculateRealizedPnL_MultipleLots_SumsCorrectly` (:276) | test | Tests B16. FIFO realized P&L is still proven through `ApplyTrade_TwoBuysOneSell_FifoClosesFirstLot` (:119) and `ApplyTrade_FullClose_PositionStatusClosed` (:166). |

## D. Tests that fail the bar (live code)

| # | Path | Kind | Evidence |
|---|---|---|---|
| D1 | `Data/SchemaMigrationTests.cs::ProductionStartup_UsesMigrationInitializer` (:42) | copy/doc pin (2) | Reads `Program.cs` as text and asserts that it contains `"DatabaseInitializer.MigrateAsync("` and not `"Database.EnsureCreated"`. The stronger proof is `DatabaseInitializer_FreshDatabase_AppliesEveryMigrationAndLeavesNonePending` (:98). |
| D2 | `Unit/Models/DataLakeArtifactTests.cs` (whole file, 1 test) | trivial (1) | Asserts C# property defaults (`AttemptCount == 0`, `ArtifactKind == "time_series_bars"`). The Backend never creates a `DataLakeArtifact` row (Python writes the table), so the defaults reach nobody. |
| D3 | `PolygonServiceTests.cs::FetchAggregatesAsync_PostsToCorrectEndpoint` (:88) | copy pin / mock theater (2/4) | Asserts that the request path equals the literal `/api/aggregates/fetch`, a string the implementation hard-codes (`PolygonService.cs:65`). The cross-stack contract test `CrossStackContractFixtureTests` is the real proof of the wire shape. |
| D4 | `PolygonServiceTests.cs::FetchAggregatesAsync_CancellationRequested_ThrowsOperationCanceled` (:222) | trivial (1) | A pre-cancelled token makes `HttpClient` throw. That is framework behavior, not ours. |
| D5 | `MarketDataServiceTests.cs::GetOrFetchAggregatesAsync_CancellationRequested_ThrowsOperationCanceled` (:495) | trivial (1) | Same as D4: it tests EF/`HttpClient` token handling. |
| D6 | `MarketDataServiceTests.cs::FetchAndStoreAggregatesAsync_NewData_InsertsAggregates` (:110) | duplicate (3) | `FetchAndStoreAggregatesAsync_MixedNewAndExisting_UpsertsBothCorrectly` (:285) seeds one existing bar and one new bar, then asserts both the insert (313) and the row count. That covers this test. |
| D7 | `MarketDataServiceTests.cs::FetchAndStoreAggregatesAsync_ExistingData_UpdatesInPlace` (:161) | duplicate (3) | The same Mixed test (:285) asserts the existing bar is updated in place (300→310) with no duplicate row. |
| D8 | `QueryTests.cs::GetOptionsExpirations_PassesFiltersThrough` (:427) | mock theater (4) | Its point is `_polygonMock.Verify(FetchOptionsExpirationsAsync(...args...))`, an argument-forwarding check. The mapping outcome is already in `GetOptionsExpirations_Success_MapsExpirationList` (:405). |
| D9 | `QueryTests.cs::PricingModelComparison_PassesNumPointsAndRange` (:652) | mock theater (4) | It only asserts `_polygonMock.Verify(PricingCompareAsync(...))` forwarded `numPoints`, `spotMin` and `spotMax`. |
| D10 | `QueryTests.cs::GetOptionsChainSnapshot_ServiceThrows_ReturnsErrorResult` (:265), `::GetOptionsExpirations_ServiceThrows_ReturnsErrorResult` (:450), `::AnalyzeOptionsStrategy_ServiceThrows_ReturnsErrorWithSymbol` (:571), `::PricingModelComparison_ServiceThrows_ReturnsErrorResult` (:684) | trivial (1) | Each mocks the service to throw and asserts `Success == false` and that `Error` contains the mock's own message. That is the same one-line `catch → new Result{Error = ex.Message}` in each resolver, and it can't fail meaningfully. |
| D11 | `PortfolioServiceTests.cs::RecordTrade_CreatesTradeAndPosition` (:143) | duplicate (3) | It asserts only that the returned trade echoes its input (price, quantity, non-empty id). `GetPortfolioState_ReturnsAccountAndPositions` (:226) records the same kind of trade and asserts the resulting position (`NetQuantity == 100`). |
| D12 | `PortfolioValuationServiceTests.cs::ComputeValuation_StockPosition_PriceTimesQuantity` (:67) | duplicate (3) | It asserts `MarketValue == 17_500` and `Equity == 67_500`. `ComputeValuation_EquityCashPlusMarketValue` (:138) asserts the same two values plus `Cash`. |
| D13 | `ResearchServiceTests.cs::GetExperimentsAsync_NoResults_ReturnsEmptyList` (:182), `::GetSignalExperimentsAsync_NoResults_ReturnsEmptyList` (:469), `::GetSignalExperimentReportAsync_NotFound_ReturnsNull` (:547) | trivial (1) | An EF `Where/ToListAsync` or `FirstOrDefaultAsync` over an empty in-memory DB returns empty or null. The `WithData`/`Found` siblings are kept. |

**Checked and kept** (they earn their place): the rest of `QueryTests` (summary math, mapping, the Phase-11 optional-curve mapping), `CrossStackContractFixtureTests`, `JobsApiTests` (redaction and control-secret outcomes), `MarketDataServiceTests` (ticker create/idempotency/market split, cache hit/miss/force-refresh "no external fetch" outcomes, upsert, rethrow-not-swallow, UTC kind), `PolygonServiceTests` error branches, all `PositionEngineTests` FIFO tests, `PortfolioReconciliationServiceTests`, `PortfolioRiskServiceTests`, the other `PortfolioValuationServiceTests`, `SnapshotServiceTests`, `SchemaMigrationTests` (other than D1), `ActivateDataRootScopedCatalogIdentityMigrationTests`, and the four legacy-input `DataLabSessionTimestampTests` (see section F for the other eight).

## E. Dead by config — rows pending one rule answer

The Backend IV recorder is opt-in and has never been switched on in any checked-in config. I did not put it in sections A–D. Map, see "Needs the map" below.

| # | Path | Kind | Evidence |
|---|---|---|---|
| E1 | `Backend/Jobs/IvRecorderJob.cs`, `Jobs/IvRecorderRegistration.cs`, `Configuration/IvRecorderOptions.cs`, `Program.cs:37-41` (`AddIvRecorder`), `appsettings.json:17-22` (`IvRecorder` section), the Quartz packages (`Backend.csproj:23-25`) | scheduled job | `AddIvRecorder` returns before scheduling unless `IvRecorder:Enabled` (`IvRecorderRegistration.cs:48-51`). `appsettings.json:18` has `"Enabled": false`. No compose file (`compose.yaml:362-370`, `compose.fleet.yaml:185-195`), `deploy/fleet/env/*.example` or workflow sets `IvRecorder__Enabled`. The introducing commit `87524c58` (2026-04-27) calls it "opt-in … Enabled=false by default", and git history shows no later commit enabling it. |
| E2 | `Backend.Tests/Unit/Jobs/IvRecorderJobTests.cs` (7 tests) | dead code's tests | Test E1 only. |

## F. Conflict that needs the map — Data Lab session int64 fields

The Data Lab session's `int64 ms UTC` fields are unreachable from the app. Cutting them would, however, move against `.claude/rules/temporal-rigor.md`, which governs until the rule rewrite lands.

- **Input:** `DataLabSessionInput.WindowStartMsUtc/WindowEndMsUtc/CreatedMsUtc/UpdatedMsUtc` (`DataLabMutation.cs:355-364`) and `ValidateTimestamps` (`DataLabMutation.cs:29-43`), which validates only those inputs. `data-lab-session.service.ts:298-313` `toInput` sends only `fromDate`/`toDate` strings, never the numeric fields.
- **Output:** no Frontend document selects `DataLabSession.windowStartMsUtc/windowEndMsUtc/createdMsUtc/updatedMsUtc` (selected-token scan). The UI reads the string `fromDate`/`toDate`.
- **Tests that ride on it:** `DataLabSessionTimestampTests.cs` `::SaveDataLabSession_ExplicitNumericInput_OverridesDerivation` (:45), `::SaveDataLabSession_RejectsInvertedWindow` (:152), `::SaveDataLabSession_RejectsOutOfRangeWindowStart` (:171), `::SaveDataLabSession_RejectsOutOfRangeCreatedMs` (:188), `::UpdateDataLabSession_RejectsInvertedWindow` (:202), `::UpdateDataLabSession_RejectsOutOfRangeTimestamp` (:222), `::UpdateDataLabSession_IgnoresCreatedMsUtcInput` (:237), `::UpdateDataLabSession_UpdatedMsUtcIsServerOwned` (:255).
- **The conflict:** by reachability, the input branch and its 8 tests are dead. The temporal rule, though, says the numeric field is the canonical wire format and calls the string path interim. Two ways to resolve it: cut the numeric input branch, or make the Frontend send and read ms. I did not pick one, so it is not on the list.

## What the cuts orphan

- **Program.cs:** the `AddHttpClient` registrations for `ITechnicalAnalysisService` (:75-82) and `ISanitizationService` (:65-72) go. With B4 gone, `ResearchService` keeps only its DB readers and no longer needs a typed `HttpClient` or `IMarketDataService` (`Program.cs:90-97` becomes a plain scoped registration; its constructor slims).
- **Test helpers:** `Helpers/FakeHttpMessageHandler.cs` stays (surviving `PolygonServiceTests`); `TestDbContextFactory` and `PostgresIntegrationTestDatabase` stay. After C11/C12, the DTO builders in `ResearchServiceTests.cs` (`QuantileBinDto` :84-87, `WalkForwardResultDto`/`GraduationResultDto`/`MethodologyDto` :338-346) become unused.
- **Packages:** if E1 is cut, `Quartz`, `Quartz.Extensions.DependencyInjection` and `Quartz.Extensions.Hosting` (`Backend.csproj:23-25`) have no user.
- **Frontend types:** the TS interfaces and document constants for every A-row (e.g. `RunRuleBasedBacktestResponse`, `RUN_RULE_BASED_BACKTEST_MUTATION`, `GET_RESEARCH_EXPERIMENT_QUERY`) belong to #2709.
- **Python endpoints that lose their only Backend caller** (see Pointers).
- **Schema snapshot:** `contracts/graphql/backend.schema.graphql` loses the 37 roots and every type reachable only from them.

## Hazards the cutting PR must carry

1. **EF migration, no hand-edited snapshot.** B19–B23 drop five entities (`Trades`, `Quotes`, `TechnicalIndicators`, `ReferenceData`, `OptionsIvSnapshots`) and two `Ticker` navigations. The PR must add a migration generated with `dotnet ef migrations add` in a container that has the SDK, since there is no `dotnet` on the host. It must never hand-edit `Backend/Migrations/AppDbContextModelSnapshot.cs`.
2. **Data in the dropped tables.** Nothing in the repo writes these tables now, but the owner's Postgres may still hold old rows (for example `OptionsIvSnapshots` from the retired options-research mutation). Before merging the drop migration, the PR should check row counts in the live DB and say what is lost.
3. **Postgres-gated migration tests.** `SchemaMigrationTests` fingerprints the schema (`RepairLegacySchemaDrift_RecreatesTheCanonicalRawSqlCatalog`, `FreshDatabase_AppliesEveryMigration…`). They are `Category=PostgresIntegration` and run daily, not on the PR path, so the PR runs them explicitly against an ephemeral database (`POSTGRES_URL_IS_EPHEMERAL=1`).
4. **GraphQL snapshot is a CI gate, so these PRs merge serially.** `ci.yml:233-234` re-exports the schema and `git diff --exit-code`s `contracts/graphql/backend.schema.graphql`. Every cutting PR regenerates it, and PRs that touch it merge one at a time.
5. **Cut Backend and Frontend together, or Frontend first.** The A-rows marked "dead wrapper" still have Frontend document constants that name the field, and a Frontend spec may still execute them against a mocked endpoint. Deleting the Backend field first leaves those constants pointing at a removed field. Land #2709's wrapper cuts in the same PR or before it.
6. **Re-check at the cutting SHA.** Every "no Frontend selection" claim is a point-in-time scan at `87b8e261`. Re-run the document parse before deleting.

## Pointers (cuttable, outside this area)

- **#2709 (rest of the Frontend):** these Frontend wrappers have no caller.
  - `market-data.service.ts`: `getFetchProgress` :529, `checkCachedRanges` :540, `getOptionsContracts` :561, `calculateIndicators` :620, `getStockSnapshots` :678, `getMarketMovers` :694, `getUnifiedSnapshot` :710, `runRuleBasedBacktest` :774.
  - `research.service.ts`: `runFeatureResearch` :1135, `runSignalEngine` :1174, `getExperiment` :1200, `runOptionsFeatureResearch` :1248, `runBatchOptionsResearch` :1270.
  - `quantlib.service.ts`: `checkStatus` :127, `priceStrategy` :172.
  - `portfolio.service.ts`: `getValuation` :119.
  - `data-lab-session.service.ts`: `updateChartSnapshot` :267.
  - The whole `ticker.service.ts`, which only its spec imports.
  - Also `Frontend/src/assets/docs/signal-engine-methodology.md`, which describes the `runSignalEngine` mutation.
  - **Also for #2709:** the Research Lab history pages (`experiment-history`, `signal-history`, `signal-report-page`) read `ResearchExperiments`/`SignalExperiments`. Nothing has written those tables since the runs moved to `/api/jobs`: the only writers are B4. If #2709 judges those pages a retired feature, then `getResearchExperiments`, `getSignalExperiments`, `getSignalExperimentReport`, the rest of `ResearchService`, `SignalResultMapper`, `Types/SignalResult.cs`, most of `SignalModels.cs`, and the two EF entities (a migration) follow them.
- **#2734 (rest of the Frontend specs):** the specs for the wrappers above (`ticker.service.spec.ts`, plus the `market-data`/`research`/`quantlib` service spec cases that exercise them).
- **#2706 (data-service HTTP routes):** Python routes whose only caller was a Backend client cut here.
  - Indicators and sanitize: `/api/indicators/calculate`, `/api/indicators/generate-table`, `/api/sanitize`.
  - Research: `/api/research/run-feature`, `/api/research/run-signal`, `/api/research/run-options-feature`, `/api/research/run-batch-options`, `/api/research/build-iv-history`.
  - Backtest: `/api/backtest/rule-based/run`.
  - Snapshots and options: `/api/snapshot/movers`, `/api/snapshot/unified`, `/api/options/contracts`, `/api/trades/fetch`.
  - QuantLib: `/api/quantlib/status`, `/api/quantlib/strategy`.
  - `/api/dataset/available` lost its Backend caller too.
  - #2706 must still check for Frontend proxy callers before cutting. If E1 is cut, `/api/iv-recorder/snapshot` also loses its only scheduler.
- **#2707 (deploy files):** `Backend/Dockerfile` and `Backend/.dockerignore`. No compose file, workflow, script or `deploy/` file builds them; compose runs the SDK image with `dotnet watch` (`compose.yaml:353,390`).
- **#2713 (authority docs):** `docs/math-sources-of-truth.md:316,318-319` rows for `TechnicalAnalysisService`, `SanitizationService` and the research fan-out. Also `docs/portfolio-management.md`, `docs/feature-runner-authority.md` and `docs/signal-engine-authority.md`, which describe the dead mutations.
- **#2711 / #2712 (one-off and architecture docs):** `docs/audits/computational-fidelity-2026-04-22.md`, `docs/audits/structural-integrity-2026-04-22.md`, `docs/architecture/iv-ownership-research.md` and `docs/architecture/numerical-authority-migration-plan.md` name the dead surfaces.
- **#2715 (CLAUDE.md):** `Backend/CLAUDE.md` file map ("14 service interfaces", Dockerfile note) goes stale after B1/B3.
- **#2716 (CI):** none new. The `ci.yml:233-234` schema gate stays: it guards a GraphQL contract.

## Not reviewed

- **Entity-backed GraphQL output fields** that no Frontend document selects. Examples: `Account.orders/trades`, `Order.orderType/limitPrice/submittedAt/filledAt`, `OptionLeg.entryIV…entryVega`, `PortfolioTrade.order/optionLeg`, `Position.lastUpdated`, `PortfolioSnapshot.marginUsed`. They are EF columns that services read, so hiding them from GraphQL would only be a `[GraphQLIgnore]` churn. I did not judge them.
- **`PortfolioValidationService.cs`** (828 lines, behind the live `runPortfolioValidation`). I did not review its 12 internal `TestN_*` cases for retired branches.
- **Branches inside live resolvers and services** (unused parameters, defaulted arguments no caller sets). I swept by symbol references, not line by line.
- **Reachability of the Frontend components** behind the live wrappers. They are assumed routed (`app.routes.ts` has `portfolio`, `research-lab`, `data-lab`, `pricing-lab`, `options-lab`). #2709 owns that.
- **`Backend/Migrations/`** (out of area; the ticket excludes it).
