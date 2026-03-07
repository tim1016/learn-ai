import { Routes } from "@angular/router";

export const routes: Routes = [
  { path: "", redirectTo: "/market-data", pathMatch: "full" },
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
    path: "lstm/train",
    loadComponent: () =>
      import("./components/lstm/train/lstm-train.component").then(
        (m) => m.LstmTrainComponent
      ),
  },
  {
    path: "lstm/validate",
    loadComponent: () =>
      import("./components/lstm/validate/lstm-validate.component").then(
        (m) => m.LstmValidateComponent
      ),
  },
  {
    path: "lstm/predictions",
    loadComponent: () =>
      import("./components/lstm/predictions/lstm-predictions.component").then(
        (m) => m.LstmPredictionsComponent
      ),
  },
  {
    path: "lstm/models",
    loadComponent: () =>
      import("./components/lstm/models/lstm-models.component").then(
        (m) => m.LstmModelsComponent
      ),
  },
  {
    path: "portfolio",
    loadComponent: () =>
      import("./components/portfolio/portfolio.component").then(
        (m) => m.PortfolioComponent
      ),
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
];
