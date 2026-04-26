import { Routes } from "@angular/router";

export const routes: Routes = [
  { path: "", redirectTo: "/market-data", pathMatch: "full" },
  {
    path: "jobs-demo",
    loadComponent: () =>
      import("./components/jobs/backtest-job-page.component").then(
        (m) => m.BacktestJobPageComponent
      ),
  },
  {
    path: "market-data",
    loadComponent: () =>
      import("./components/market-data/market-data.component").then(
        (m) => m.MarketDataComponent
      ),
  },
  {
    path: "tickers",
    loadComponent: () =>
      import("./components/tickers/tickers.component").then(
        (m) => m.TickersComponent
      ),
  },
  {
    path: "technical-analysis",
    loadComponent: () =>
      import(
        "./components/tickers/technical-analysis/technical-analysis.component"
      ).then((m) => m.TechnicalAnalysisComponent),
  },
  {
    path: "stock-analysis",
    loadComponent: () =>
      import("./components/stock-analysis/stock-analysis.component").then(
        (m) => m.StockAnalysisComponent
      ),
  },
  {
    path: "stock-analysis/chunk/:ticker/:fromDate/:toDate",
    loadComponent: () =>
      import(
        "./components/stock-analysis/chunk-detail/chunk-detail.component"
      ).then((m) => m.ChunkDetailComponent),
  },
  {
    path: "stock-analysis/day/:ticker/:date",
    loadComponent: () =>
      import(
        "./components/stock-analysis/day-detail/day-detail.component"
      ).then((m) => m.DayDetailComponent),
  },
  {
    path: "options-chain",
    loadComponent: () =>
      import("./components/options-chain-v2/options-chain.component").then(
        (m) => m.OptionsChainComponent
      ),
  },
  {
    path: "strategy-lab",
    loadComponent: () =>
      import("./components/strategy-lab/strategy-lab.component").then(
        (m) => m.StrategyLabComponent
      ),
  },
  {
    path: "strategy-lab-validation",
    loadComponent: () =>
      import(
        "./components/strategy-lab-validation/strategy-lab-validation.component"
      ).then((m) => m.StrategyLabValidationComponent),
  },
  {
    path: "strategy-docs",
    loadComponent: () =>
      import(
        "./components/strategy-lab/strategy-docs/strategy-docs.component"
      ).then((m) => m.StrategyDocsComponent),
  },
  {
    path: "options-strategy-lab",
    loadComponent: () =>
      import(
        "./components/options-strategy-lab/options-strategy-lab.component"
      ).then((m) => m.OptionsStrategyLabComponent),
  },
  {
    path: "strategy-builder",
    loadComponent: () =>
      import("./components/strategy-builder/strategy-builder.component").then(
        (m) => m.StrategyBuilderComponent
      ),
  },
  {
    path: "pricing-lab",
    loadComponent: () =>
      import("./components/pricing-lab/pricing-lab.component").then(
        (m) => m.PricingLabComponent
      ),
  },
  {
    path: "options-history",
    loadComponent: () =>
      import("./components/options-history/options-history.component").then(
        (m) => m.OptionsHistoryComponent
      ),
  },
  {
    path: "snapshots",
    loadComponent: () =>
      import("./components/snapshots/snapshots.component").then(
        (m) => m.SnapshotsComponent
      ),
  },
  {
    path: "tracked-instruments",
    loadComponent: () =>
      import(
        "./components/tracked-instruments/tracked-instruments.component"
      ).then((m) => m.TrackedInstrumentsComponent),
  },
  {
    path: "portfolio",
    loadComponent: () =>
      import("./components/portfolio/portfolio.component").then(
        (m) => m.PortfolioComponent
      ),
  },
  {
    path: "indicator-validation",
    loadComponent: () =>
      import(
        "./components/indicator-validation/indicator-validation.component"
      ).then((m) => m.IndicatorValidationComponent),
  },
  {
    path: "indicator-docs",
    redirectTo: "data-lab-docs",
    pathMatch: "full",
  },
  {
    path: "data-lab",
    loadComponent: () =>
      import("./components/data-lab/data-lab.component").then(
        (m) => m.DataLabComponent
      ),
  },
  {
    path: "data-lab-docs",
    loadComponent: () =>
      import(
        "./components/data-lab/data-lab-docs/data-lab-docs.component"
      ).then((m) => m.DataLabDocsComponent),
  },
  {
    path: "indicator-report",
    loadComponent: () =>
      import(
        "./components/indicator-validation/indicator-report/indicator-report.component"
      ).then((m) => m.IndicatorReportComponent),
  },
  {
    path: "data-quality",
    redirectTo: "data-lab",
    pathMatch: "full",
  },
  {
    path: "data-quality-docs",
    loadComponent: () =>
      import(
        "./components/data-quality/data-quality-docs/data-quality-docs.component"
      ).then((m) => m.DataQualityDocsComponent),
  },
  {
    path: "engine",
    loadComponent: () =>
      import("./components/lean-engine/lean-engine.component").then(
        (m) => m.LeanEngineComponent
      ),
  },
  {
    path: "engine/docs",
    redirectTo: "engine",
    pathMatch: "full",
  },
  {
    path: "lean-engine",
    redirectTo: "engine",
    pathMatch: "full",
  },
  {
    path: "research-lab",
    loadComponent: () =>
      import("./components/research-lab/research-lab.component").then(
        (m) => m.ResearchLabComponent
      ),
  },
  {
    path: "research-lab/signal-report/:id",
    loadComponent: () =>
      import(
        "./components/research-lab/signal-report-page/signal-report-page.component"
      ).then((m) => m.SignalReportPageComponent),
  },
  {
    path: "docs/indicator-reliability-methodology",
    loadComponent: () =>
      import("./components/docs/methodology-page.component").then(
        (m) => m.MethodologyPageComponent
      ),
  },
  {
    path: "edge",
    loadComponent: () =>
      import("./components/edge/edge.component").then((m) => m.EdgeComponent),
    children: [
      {
        path: "realized-vs-iv",
        loadComponent: () =>
          import(
            "./components/edge/realized-vs-iv/realized-vs-iv.component"
          ).then((m) => m.RealizedVsIvComponent),
      },
      {
        path: "cross-asset",
        loadComponent: () =>
          import("./components/edge/cross-asset/cross-asset.component").then(
            (m) => m.CrossAssetComponent
          ),
      },
      {
        path: "regimes",
        loadComponent: () =>
          import("./components/edge/regimes/regimes.component").then(
            (m) => m.RegimesComponent
          ),
      },
    ],
  },
];
