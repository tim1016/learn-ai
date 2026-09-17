import { inject } from "@angular/core";
import { Router, Routes, type RedirectFunction } from "@angular/router";
import { alpacaSurfaceRedirectGuard } from "./fleet/alpaca-surface-redirect.guard";
import { brokerClerkRedirectGuard } from "./fleet/broker-clerk-redirect.guard";

const loadBrokerLaneUnavailable = () =>
  import('./fleet/broker-lane-unavailable.component').then(
    (module) => module.BrokerLaneUnavailableComponent,
  );

// Shared by every legacy persisted-run URL (strategy-lab/runs/:id and the
// older engine/runs/:id bookmark). Both must redirect straight to this same
// final destination rather than to each other: Angular's router does not
// chain a redirect target that is itself a redirect route within one
// navigation, so a two-hop redirect silently falls through to the wildcard.
const redirectToStrategyLabRun: RedirectFunction = ({ params }) =>
  inject(Router).parseUrl(`/strategy-lab?run=${encodeURIComponent(String(params["id"] ?? ""))}`);

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
  { path: "broker/paper-run", redirectTo: "brokers/alpaca", pathMatch: "full" },
  { path: "broker/instances", redirectTo: "brokers/alpaca", pathMatch: "full" },
  { path: "broker/instances/:id", redirectTo: "brokers/alpaca", pathMatch: "full" },
  { path: "broker/bots", redirectTo: "brokers/alpaca", pathMatch: "full" },
  { path: "broker/bots/:id", redirectTo: "brokers/alpaca", pathMatch: "full" },
  { path: "broker/offline-replay", redirectTo: "brokers/alpaca", pathMatch: "full" },
  { path: "broker/deploy", redirectTo: "brokers/alpaca", pathMatch: "full" },
];

export const routes: Routes = [
  { path: "", redirectTo: "/data-lab", pathMatch: "full" },
  { path: "lean-lab", redirectTo: "strategy-lab", pathMatch: "full" },
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
    data: { fullBleed: true },
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
    // Data Lab shell (explore / export / validate child routes). The route
    // config owns the component-scoped workspace store — see
    // components/data-lab/data-lab.routes.ts (PRD 2026-09-12 §7.1).
    path: "data-lab",
    loadChildren: () =>
      import("./components/data-lab/data-lab.routes").then(
        (m) => m.DATA_LAB_ROUTES
      ),
  },
  {
    // Data-lake catalog surface. This was flag-gated until #1893 retired
    // DATA_LAKE_ENABLED; the routes are always mounted now, so the page has
    // no "not enabled" state to render.
    path: "data-lake",
    loadComponent: () =>
      import(
        "./components/data-lake-observatory/data-lake-observatory.component"
      ).then((m) => m.DataLakeObservatoryComponent),
  },
  {
    path: "data-lab-docs",
    loadComponent: () =>
      import(
        "./components/data-lab/data-lab-docs/data-lab-docs.component"
      ).then((m) => m.DataLabDocsComponent),
  },
  {
    // Legacy /data-quality bookmark → the Validate child route (PRD §7.1).
    // Any query params pass through; the Data Lab shell normalizes legacy
    // query state after landing.
    path: "data-quality",
    redirectTo: "data-lab/validate",
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
    // The workbench and this URL must be the SAME route config: a different
    // one would destroy and recreate StrategyLabComponent, tearing down its
    // component-scoped config store and runner while a run is in flight.
    path: "strategy-lab/runs/:id",
    redirectTo: redirectToStrategyLabRun,
    pathMatch: "full",
  },
  {
    path: "strategy-lab/docs",
    loadComponent: () =>
      import("./components/strategy-lab/analytical-manual/strategy-lab-analytical-manual.component").then(
        (m) => m.StrategyLabAnalyticalManualComponent
      ),
  },
  {
    path: "strategy-lab",
    data: { fullBleed: true },
    loadComponent: () =>
      import("./components/strategy-lab/strategy-lab.component").then(
        (m) => m.StrategyLabComponent
      ),
  },
  {
    path: "grid-search",
    loadComponent: () =>
      import("./components/grid-search/grid-search-page.component").then(
        (m) => m.GridSearchPageComponent
      ),
  },
  {
    path: "walk-forward",
    loadComponent: () =>
      import("./components/walk-forward-study/walk-forward-study-page.component").then(
        (m) => m.WalkForwardStudyPageComponent
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
    redirectTo: "strategy-lab/docs",
    pathMatch: "full",
  },
  {
    path: "engine-docs",
    redirectTo: "strategy-lab/docs",
    pathMatch: "full",
  },
  {
    // Redirects straight to the final destination (not to strategy-lab/runs/:id)
    // for the same reason as that route: Angular does not chain a redirect
    // through another redirect route within one navigation.
    path: "engine/runs/:id",
    redirectTo: redirectToStrategyLabRun,
    pathMatch: "full",
  },
  {
    path: "engine",
    redirectTo: "strategy-lab",
    pathMatch: "full",
  },
  {
    path: "lean-engine",
    redirectTo: "strategy-lab",
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
    path: "legal/notices",
    loadComponent: () =>
      import(
        "./components/legal/legal-notices-page/legal-notices-page.component"
      ).then((m) => m.LegalNoticesPageComponent),
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
  // Deploy bookmarks lack a clerk identity. They stay visible as an explicit
  // failure rather than silently opening the newly selected lane's drawer.
  {
    path: "brokers/alpaca/accounts/:accountId/deploy",
    loadComponent: loadBrokerLaneUnavailable,
  },
  {
    path: "brokers/alpaca/deploy",
    loadComponent: loadBrokerLaneUnavailable,
  },
  {
    // User-owned broker configuration profiles (ADR 0060). Declared before the
    // desk so the intent of the deeper path is readable next to it; Angular
    // would backtrack to it either way, since the desk route consumes no
    // trailing segments.
    //
    // Delivery C: configuration is canonical only under an explicit clerk.
    path: "brokers/alpaca/configuration",
    loadComponent: loadBrokerLaneUnavailable,
  },
  {
    // ── Fleet clerk-scoped canonical routes (PRD §13/FR-092) ────────────────
    // The lane's configuration surface — repair stays reachable without a
    // confirmed binding (configuration-access readiness).
    path: "brokers/alpaca/clerks/:clerkId/configuration",
    loadComponent: () =>
      import(
        "./components/brokers/alpaca-desk/configuration/alpaca-configuration-page.component"
      ).then((m) => m.AlpacaConfigurationPageComponent),
  },
  {
    // A clerk-only surface URL: the lane's in-place explanation for why its
    // Bots roster cannot open (not ready, unbound, or without the
    // capability). Selectable from the choosers; never redirected to another
    // lane (FR-096).
    path: "brokers/alpaca/clerks/:clerkId/bots",
    data: { broker: "alpaca", surface: "bots" },
    loadComponent: () =>
      import(
        "./components/brokers/alpaca-desk/lane-directory/alpaca-clerk-surface-unavailable.component"
      ).then((m) => m.AlpacaClerkSurfaceUnavailableComponent),
  },
  {
    // The Gallery twin of the clerk-only surface route above.
    path: "brokers/alpaca/clerks/:clerkId/gallery",
    data: { broker: "alpaca", surface: "gallery" },
    loadComponent: () =>
      import(
        "./components/brokers/alpaca-desk/lane-directory/alpaca-clerk-surface-unavailable.component"
      ).then((m) => m.AlpacaClerkSurfaceUnavailableComponent),
  },
  {
    // A lane deep link without a surface: its configuration is the lane's
    // own home (the desk directory is the broker-level surface).
    path: "brokers/alpaca/clerks/:clerkId",
    redirectTo: "configuration",
    pathMatch: "full",
  },
  {
    // The bot panel, addressed by broker + clerk + account + bot identity.
    path: "brokers/alpaca/clerks/:clerkId/accounts/:accountId/bots/:sid",
    data: { fullBleed: true, broker: 'alpaca' },
    loadComponent: () =>
      import(
        "./components/broker/v2-panel/panel-shell/bot-panel-shell.component"
      ).then((m) => m.BotPanelShellComponent),
  },
  {
    // ── The account workspace (ADR 0064 Decision 1) ─────────────────────────
    // One account is one place: the account header and its tabs are this
    // parent, and each tab is a child, so moving between them never
    // re-creates the header or the shared account read. The canonical URLs
    // are unchanged — Overview is still the bare account URL (FR-092) — and
    // `:clerkId`/`:accountId` reach every child through the router's
    // `paramsInheritanceStrategy: 'always'` (see `app.config.ts`).
    //
    // Declared AFTER `…/bots/:sid` above: a bot's own page is not a
    // workspace tab yet (it moves inside in a later slice), so it must match
    // its own route first.
    //
    // Full-bleed on the PARENT, not on one tab: the header and the tab strip
    // are the workspace's own chrome and must sit at the same place on every
    // tab. Declaring it per tab gave the shell's page inset to some tabs and
    // not others, which moved the header ~24px when the operator switched to
    // Gallery. Each tab now owns whatever inset its own content wants.
    path: 'brokers/alpaca/clerks/:clerkId/accounts/:accountId',
    data: { fullBleed: true, broker: 'alpaca' },
    loadComponent: () =>
      import(
        './components/brokers/alpaca-workspace/alpaca-account-workspace.component'
      ).then((m) => m.AlpacaAccountWorkspaceComponent),
    children: [
      {
        path: 'bots',
        loadComponent: () =>
          import(
            './components/broker/v2-panel/bots-list-page/bots-list-page.component'
          ).then((m) => m.BotsListPageComponent),
      },
      {
        // The wall stays edge to edge under the workspace header — as every
        // tab now does, from the parent's `fullBleed` above.
        path: 'gallery',
        loadComponent: () =>
          import(
            './components/broker/v2-panel/gallery/bot-gallery-page/bot-gallery-page.component'
          ).then((m) => m.BotGalleryPageComponent),
      },
      {
        // Overview — the empty child, so the account's own URL opens it.
        path: '',
        loadComponent: () =>
          import('./components/brokers/alpaca-desk/alpaca-desk.component').then(
            (m) => m.AlpacaDeskComponent,
          ),
      },
    ],
  },
  {
    // Read-only lane choosers: every Alpaca lane listed side by side, each
    // linking to its own canonical operational URL (or its clerk-only
    // explanation). No lane is ever selected automatically (FR-096).
    path: "brokers/alpaca/bots",
    data: { broker: "alpaca", surface: "bots" },
    loadComponent: () =>
      import("./components/brokers/alpaca-desk/alpaca-surface-chooser.component").then(
        (m) => m.AlpacaSurfaceChooserComponent,
      ),
  },
  {
    path: "brokers/alpaca/gallery",
    data: { fullBleed: true, broker: "alpaca", surface: "gallery" },
    loadComponent: () =>
      import("./components/brokers/alpaca-desk/alpaca-surface-chooser.component").then(
        (m) => m.AlpacaSurfaceChooserComponent,
      ),
  },
  {
    // The account list — the only multi-account page (ADR 0064 Decision 2).
    // Retires the old `?surface=bots|gallery` hint bookmarks by redirecting
    // them to the real chooser routes above.
    path: "brokers/alpaca",
    canActivate: [alpacaSurfaceRedirectGuard],
    loadComponent: () =>
      import("./components/brokers/alpaca-desk/alpaca-account-list-page.component").then(
        (m) => m.AlpacaAccountListPageComponent,
      ),
  },
  {
    // Broker-v2 bot control panel — the canonical panel route is
    // clerk-scoped above; this unscoped URL redirects through lane
    // resolution (FR-096: an unresolvable account fails to the directory,
    // never to another lane).
    path: "brokers/:broker/accounts/:accountId/bots/:sid",
    canActivate: [brokerClerkRedirectGuard('/bots/:sid')],
    loadComponent: loadBrokerLaneUnavailable,
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
    // Broker v2 panel — account-scoped bots list: the canonical roster route
    // is clerk-scoped above; this unscoped URL redirects through the lane.
    path: 'brokers/:broker/accounts/:accountId/bots',
    canActivate: [brokerClerkRedirectGuard('/bots')],
    loadComponent: loadBrokerLaneUnavailable,
  },
  {
    // Broker v2 panel — live gallery wall: the canonical gallery route is
    // clerk-scoped above; this unscoped URL redirects through the lane.
    path: 'brokers/:broker/accounts/:accountId/gallery',
    canActivate: [brokerClerkRedirectGuard('/gallery')],
    loadComponent: loadBrokerLaneUnavailable,
  },
  // A canonical-looking route for another provider is still the same URL
  // after refusal. Literal Alpaca routes above win first; these parametric
  // fallbacks prevent wrong-provider links from reaching the app wildcard.
  {
    path: 'brokers/:broker/clerks/:clerkId/configuration',
    loadComponent: loadBrokerLaneUnavailable,
  },
  {
    path: 'brokers/:broker/clerks/:clerkId/accounts/:accountId/bots/:sid',
    loadComponent: loadBrokerLaneUnavailable,
  },
  {
    path: 'brokers/:broker/clerks/:clerkId/accounts/:accountId/bots',
    loadComponent: loadBrokerLaneUnavailable,
  },
  {
    path: 'brokers/:broker/clerks/:clerkId/accounts/:accountId/gallery',
    loadComponent: loadBrokerLaneUnavailable,
  },
  {
    path: 'brokers/:broker/clerks/:clerkId/accounts/:accountId',
    loadComponent: loadBrokerLaneUnavailable,
  },
  {
    path: 'brokers/:broker/clerks/:clerkId',
    loadComponent: loadBrokerLaneUnavailable,
  },
  {
    // Unscoped operational compatibility URLs cannot prove an account lane.
    path: 'brokers/:broker/bots',
    loadComponent: loadBrokerLaneUnavailable,
  },
  {
    path: 'brokers/:broker/gallery',
    loadComponent: loadBrokerLaneUnavailable,
  },
  { path: "**", redirectTo: "/data-lab" },
];
