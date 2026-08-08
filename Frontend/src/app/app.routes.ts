import { ChangeDetectionStrategy, Component } from "@angular/core";
import { Routes } from "@angular/router";
import { brokerBotsRedirectGuard } from "./components/broker/v2-panel/lib/broker-bots-redirect.guard";

// Nominal component target for the brokerBotsRedirectGuard route.
// The guard always returns a UrlTree so this component never renders.
@Component({ template: '', changeDetection: ChangeDetectionStrategy.OnPush })
class NeverRendersComponent {}

/**
 * Retired Interactive Broker navigation.
 *
 * These compatibility aliases preserve bookmarked URLs while directing users
 * to the sole supported broker-control product: Alpaca Broker V2. They must
 * remain redirect-only; do not attach UI, providers, guards, or new behavior.
 */
const RETIRED_IBKR_NAVIGATION_ROUTES: Routes = [
  { path: "broker", redirectTo: "brokers/alpaca", pathMatch: "full" },
  { path: "broker/accounts", redirectTo: "brokers/alpaca", pathMatch: "full" },
  { path: "broker/accounts/:accountId", redirectTo: "brokers/alpaca", pathMatch: "full" },
  { path: "broker/account-monitor", redirectTo: "brokers/alpaca", pathMatch: "full" },
  { path: "broker/reconciliation", redirectTo: "brokers/alpaca", pathMatch: "full" },
  { path: "broker/orders", redirectTo: "brokers/alpaca", pathMatch: "full" },
  { path: "broker/session-mirror", redirectTo: "brokers/alpaca", pathMatch: "full" },
  { path: "broker/paper-run", redirectTo: "brokers/alpaca/bots", pathMatch: "full" },
  { path: "broker/instances", redirectTo: "brokers/alpaca/bots", pathMatch: "full" },
  { path: "broker/instances/:id", redirectTo: "brokers/alpaca/bots", pathMatch: "full" },
  { path: "broker/bots", redirectTo: "brokers/alpaca/bots", pathMatch: "full" },
  { path: "broker/bots/:id", redirectTo: "brokers/alpaca/bots", pathMatch: "full" },
  { path: "broker/offline-replay", redirectTo: "brokers/alpaca", pathMatch: "full" },
  { path: "broker/bot-manual", redirectTo: "brokers/alpaca/manual", pathMatch: "full" },
  { path: "broker/deploy", redirectTo: "brokers/alpaca/deploy", pathMatch: "full" },
];

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
    // Unlinked, fixture-only review surface. It imports committed contracts
    // locally and deliberately has no data service or mutation path.
    path: "examples/alpaca-bot-control",
    loadComponent: () =>
      import(
        "./components/examples/alpaca-bot-control/alpaca-bot-control-example.component"
      ).then((m) => m.AlpacaBotControlExampleComponent),
  },
  {
    // Broker-aware deployment uses the broker-v2 account-scoped contract.
    path: "brokers/:broker/accounts/:accountId/deploy",
    loadComponent: () =>
      import(
        "./components/broker/broker-deploy-page/broker-deploy-page.component"
      ).then((m) => m.BrokerDeployPageComponent),
  },
  {
    path: "brokers/:broker/deploy",
    loadComponent: () =>
      import(
        "./components/broker/broker-deploy-page/broker-deploy-page.component"
      ).then((m) => m.BrokerDeployPageComponent),
  },
  {
    // Broker System v2 read-only desk — separate from every v1 broker page.
    path: "brokers/alpaca",
    loadComponent: () =>
      import("./components/brokers/alpaca-desk/alpaca-desk.component").then(
        (m) => m.AlpacaDeskComponent,
      ),
  },
  {
    // Broker-v2 bot control panel — trader lens (S3); operator lens adds in S4.
    // Route binds broker, accountId, sid as component inputs.
    path: "brokers/:broker/accounts/:accountId/bots/:sid",
    loadComponent: () =>
      import(
        "./components/broker/v2-panel/panel-shell/bot-panel-shell.component"
      ).then((m) => m.BotPanelShellComponent),
  },
  {
    path: "brokers/:broker/manual",
    loadComponent: () =>
      import(
        "./components/broker/v2-panel/manual-page/broker-v2-manual-page.component"
      ).then((m) => m.BrokerV2ManualPageComponent),
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
  ...RETIRED_IBKR_NAVIGATION_ROUTES,
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
  {
    // Broker v2 panel — account-scoped bots list
    path: 'brokers/:broker/accounts/:accountId/bots',
    loadComponent: () =>
      import(
        './components/broker/v2-panel/bots-list-page/bots-list-page.component'
      ).then((m) => m.BotsListPageComponent),
  },
  {
    // Broker v2 panel — unscoped bots entry point: resolves account then
    // redirects to /brokers/:broker/accounts/:accountId/bots.
    path: 'brokers/:broker/bots',
    canActivate: [brokerBotsRedirectGuard],
    component: NeverRendersComponent,
  },
  { path: "**", redirectTo: "/data-lab" },
];
