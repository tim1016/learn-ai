import { Routes } from "@angular/router";
import { BotSurfaceStore } from "./components/broker/bot-control/bot-surface-store.service";
import { AccountDeskHoldingsStore } from "./components/broker/account-desk/account-desk-holdings-store.service";
import { AccountDeskEventsStore } from "./components/broker/account-desk/account-desk-events-store.service";
import { AccountDeskDirectoryStore } from "./components/broker/account-desk/account-desk-directory-store.service";
import { AccountDeskFleetStore } from "./components/broker/account-desk/account-desk-fleet-store.service";
import { AccountDeskGuidanceStore } from "./components/broker/account-desk/account-desk-guidance-store.service";
import { AccountDeskRecoveryStore } from "./components/broker/account-desk/account-desk-recovery-store.service";
import { AccountDeskSurfaceStore } from "./components/broker/account-desk/account-desk-surface-store.service";
import {
  botExistsGuard,
  botSurfaceResolver,
} from "./components/broker/bot-control/bot-surface-routing";

export const routes: Routes = [
  { path: "", redirectTo: "/data-lab", pathMatch: "full" },
  // PR B.5 (2026-05-19) — /lean-lab is retired; the LEAN sidecar
  // launch surface now lives behind the Engine dropdown on /engine.
  // Prefix match covers any operator-bookmarked sub-paths.
  { path: "lean-lab", redirectTo: "engine", pathMatch: "prefix" },
  {
    path: "jobs-demo",
    loadComponent: () =>
      import("./components/jobs/backtest-job-page.component").then(
        (m) => m.BacktestJobPageComponent
      ),
  },
  {
    path: "strategy-docs",
    loadComponent: () =>
      import("./components/strategy-docs/strategy-docs.component").then(
        (m) => m.StrategyDocsComponent
      ),
  },
  {
    path: "options-lab",
    loadComponent: () =>
      import("./components/options-lab/options-lab.component").then(
        (m) => m.OptionsLabComponent
      ),
    children: [
      { path: "", redirectTo: "chain", pathMatch: "full" },
      {
        path: "chain",
        loadComponent: () =>
          import(
            "./components/options-lab/chain/options-lab-chain.component"
          ).then((m) => m.OptionsLabChainComponent),
      },
      {
        path: "strategy-builder",
        loadComponent: () =>
          import(
            "./components/strategy-builder/strategy-builder.component"
          ).then((m) => m.StrategyBuilderComponent),
      },
      {
        path: "strategy-finder",
        loadComponent: () =>
          import(
            "./components/options-lab/strategy-finder-stub/strategy-finder-stub.component"
          ).then((m) => m.StrategyFinderStubComponent),
      },
      {
        path: "volatility",
        loadComponent: () =>
          import(
            "./components/options-lab/volatility-stub/volatility-stub.component"
          ).then((m) => m.VolatilityStubComponent),
      },
    ],
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
    path: "engine/runs/:id",
    loadComponent: () =>
      import("./components/engine-lab/run-detail/engine-run-detail.component").then(
        (m) => m.EngineRunDetailComponent
      ),
  },
  {
    path: "engine",
    loadComponent: () =>
      import("./components/lean-engine/lean-engine.component").then(
        (m) => m.LeanEngineComponent
      ),
  },
  {
    path: "strategy-validation",
    loadComponent: () =>
      import(
        "./components/strategy-validation/strategy-validation.component"
      ).then((m) => m.StrategyValidationComponent),
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
    loadChildren: () =>
      import("./components/research-lab/research-lab.routes").then(
        (m) => m.researchLabRoutes
      ),
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
    path: "docs/ibkr-setup-guide",
    loadComponent: () =>
      import("./components/docs/ibkr-setup-guide-page.component").then(
        (m) => m.IbkrSetupGuidePageComponent
      ),
  },
  {
    path: "broker",
    redirectTo: "broker/accounts",
    pathMatch: "full",
  },
  {
    path: "broker/options-chain",
    loadComponent: () =>
      import(
        "./components/broker/broker-options-chain/broker-options-chain.component"
      ).then((m) => m.BrokerOptionsChainComponent),
  },
  {
    path: "broker/options-surface",
    loadComponent: () =>
      import(
        "./components/broker/broker-options-surface/broker-options-surface.component"
      ).then((m) => m.BrokerOptionsSurfaceComponent),
  },
  {
    path: "broker/accounts",
    providers: [AccountDeskDirectoryStore],
    loadComponent: () =>
      import(
        "./components/broker/account-roster/account-roster-page.component"
      ).then((m) => m.AccountRosterPageComponent),
  },
  {
    path: "broker/accounts/:accountId",
    providers: [AccountDeskSurfaceStore, AccountDeskHoldingsStore, AccountDeskEventsStore, AccountDeskDirectoryStore, AccountDeskFleetStore, AccountDeskGuidanceStore, AccountDeskRecoveryStore],
    loadComponent: () =>
      import(
        "./components/broker/account-desk/account-desk-page.component"
      ).then((m) => m.AccountDeskPageComponent),
  },
  {
    path: "broker/account-monitor",
    loadComponent: () =>
      import(
        "./components/broker/account-monitor-redirect/account-monitor-redirect.component"
      ).then((m) => m.AccountMonitorRedirectComponent),
  },
  {
    path: "broker/reconciliation",
    redirectTo: "broker/accounts",
    pathMatch: "full",
  },
  {
    path: "broker/orders",
    loadComponent: () =>
      import(
        "./components/broker/broker-orders/broker-orders.component"
      ).then((m) => m.BrokerOrdersComponent),
  },
  {
    path: "broker/session-mirror",
    loadComponent: () =>
      import(
        "./components/broker/broker-session-mirror/broker-session-mirror.component"
      ).then((m) => m.BrokerSessionMirrorComponent),
  },
  {
    path: "broker/paper-run",
    redirectTo: "broker/bots",
    pathMatch: "full",
  },
  {
    path: "broker/instances",
    redirectTo: "broker/bots",
    pathMatch: "full",
  },
  {
    path: "broker/bots",
    loadComponent: () =>
      import("./components/broker/bots/bots-page.component").then(
        (m) => m.BotsPageComponent
      ),
  },
  {
    path: "broker/bot-manual",
    loadComponent: () =>
      import(
        "./components/broker/bot-operator-manual/bot-operator-manual-page.component"
      ).then((m) => m.BotOperatorManualPageComponent),
  },
  {
    path: "broker/desert-oasis",
    loadComponent: () =>
      import(
        "./components/broker/desert-oasis-showcase/desert-oasis-showcase.component"
      ).then((m) => m.DesertOasisShowcaseComponent),
  },
  {
    path: "broker/bot-sprites",
    loadComponent: () =>
      import(
        "./components/broker/bot-sprite-gallery/bot-sprite-gallery.component"
      ).then((m) => m.BotSpriteGalleryComponent),
  },
  {
    path: "broker/bots/:id",
    providers: [BotSurfaceStore],
    canActivate: [botExistsGuard],
    resolve: { botSurface: botSurfaceResolver },
    loadComponent: () =>
      import(
        "./components/broker/bot-control/bot-control-page.component"
      ).then((m) => m.BotControlPageComponent),
  },
  {
    path: "broker/instances/:id",
    redirectTo: "broker/bots/:id",
    pathMatch: "full",
  },
  {
    // Deploy form — stage 1 of the deploy pipeline (ADR 0006, #417).
    path: "broker/deploy",
    loadComponent: () =>
      import(
        "./components/broker/broker-deploy-form/broker-deploy-form.component"
      ).then((m) => m.BrokerDeployFormComponent),
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
  {
    path: "golden-fixtures",
    loadComponent: () =>
      import(
        "./components/golden-fixtures/golden-fixtures-catalog.component"
      ).then((m) => m.GoldenFixturesCatalogComponent),
  },
  {
    path: "_ide-sandbox",
    loadComponent: () =>
      import(
        "./components/_ide-sandbox/ide-sandbox.component"
      ).then((m) => m.IdeSandboxComponent),
  },
  { path: "**", redirectTo: "/data-lab" },
];
