import { Routes } from "@angular/router";

export const routes: Routes = [
  { path: "", redirectTo: "/data-lab", pathMatch: "full" },
  {
    path: "jobs-demo",
    loadComponent: () =>
      import("./components/jobs/backtest-job-page.component").then(
        (m) => m.BacktestJobPageComponent
      ),
  },
  {
    // Per docs/architecture/options-routes-research.md §7 D9 + §5.1 R0b:
    // /options-chain is deleted; /strategy-builder absorbs the chain
    // viewer + per-contract drill-down (UX-Q1) + Greek toggle (UX-Q2).
    // Redirect kept for ≥ 7 days to preserve bookmarks; remove in
    // Phase 4.5.
    path: "options-chain",
    redirectTo: "/strategy-builder",
    pathMatch: "full",
  },
  {
    path: "strategy-docs",
    loadComponent: () =>
      import("./components/strategy-docs/strategy-docs.component").then(
        (m) => m.StrategyDocsComponent
      ),
  },
  {
    // Per docs/architecture/options-routes-research.md §7 D8 + §5.1 R0a:
    // /options-strategy-lab is deleted; /strategy-builder is the survivor.
    // Redirect kept for ≥ 7 days to preserve bookmarks; remove in Phase 4.5.
    path: "options-strategy-lab",
    redirectTo: "/strategy-builder",
    pathMatch: "full",
  },
  {
    path: "strategy-builder",
    loadComponent: () =>
      import("./components/strategy-builder/strategy-builder.component").then(
        (m) => m.StrategyBuilderComponent
      ),
  },
  {
    path: "spec-strategy",
    loadComponent: () =>
      import(
        "./components/spec-strategy-runner/spec-strategy-runner.component"
      ).then((m) => m.SpecStrategyRunnerComponent),
  },
  {
    path: "pricing-lab",
    loadComponent: () =>
      import("./components/pricing-lab/pricing-lab.component").then(
        (m) => m.PricingLabComponent
      ),
  },
  {
    // Per docs/architecture/options-routes-research.md §7 D10 + §5.1 R1:
    // /options-history is deleted; the past-chain inspector is hosted
    // inside /data-lab as a card on the options-companion config row
    // (UX-Q3). Redirect kept for ≥ 7 days to preserve bookmarks;
    // remove in Phase 4.5.
    path: "options-history",
    redirectTo: "/data-lab",
    pathMatch: "full",
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
        "./components/indicator-report/indicator-report.component"
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
    path: "docs/signal-engine-methodology",
    loadComponent: () =>
      import("./components/docs/signal-engine-methodology-page.component").then(
        (m) => m.SignalEngineMethodologyPageComponent
      ),
  },
  {
    path: "broker",
    loadComponent: () =>
      import(
        "./components/broker/broker-status/broker-status.component"
      ).then((m) => m.BrokerStatusComponent),
  },
  {
    path: "broker/options-chain",
    loadComponent: () =>
      import(
        "./components/broker/broker-options-chain/broker-options-chain.component"
      ).then((m) => m.BrokerOptionsChainComponent),
  },
  {
    path: "broker/account-monitor",
    loadComponent: () =>
      import(
        "./components/broker/broker-account-monitor/broker-account-monitor.component"
      ).then((m) => m.BrokerAccountMonitorComponent),
  },
  {
    path: "broker/orders",
    loadComponent: () =>
      import(
        "./components/broker/broker-orders/broker-orders.component"
      ).then((m) => m.BrokerOrdersComponent),
  },
  {
    path: "broker/reconciliation",
    loadComponent: () =>
      import(
        "./components/broker/broker-reconciliation/broker-reconciliation.component"
      ).then((m) => m.BrokerReconciliationComponent),
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
