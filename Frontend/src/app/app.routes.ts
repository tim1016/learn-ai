import { inject } from "@angular/core";
import { Router, Routes, type RedirectFunction, type Route } from "@angular/router";
import { alpacaSurfaceRedirectGuard } from "./fleet/alpaca-surface-redirect.guard";
import { homeRedirectGuard } from "./fleet/home-redirect.guard";
import { dropRetiredLensGuard } from "./fleet/retired-lens.guard";
import { brokerClerkRedirectGuard } from "./fleet/broker-clerk-redirect.guard";

const loadBrokerLaneUnavailable = () =>
  import('./fleet/broker-lane-unavailable.component').then(
    (module) => module.BrokerLaneUnavailableComponent,
  );

// A served document copies a canonical repo document; the copies listed in
// `scripts/check_documentation_contract.py` fail CI when the two differ.
const loadMarkdownDocPage = () =>
  import('./components/docs/markdown-doc-page.component').then(
    (module) => module.MarkdownDocPageComponent,
  );

// The page's inputs arrive as route data, so a missing key would bind
// `undefined` and render a blank page; typing the data makes it a compile error.
interface MarkdownDocRouteData {
  readonly heading: string;
  readonly src: `/assets/docs/${string}.md`;
}

const markdownDocRoute = (path: string, data: MarkdownDocRouteData): Route => ({
  path,
  loadComponent: loadMarkdownDocPage,
  data,
});

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
  // Served copy of docs/architecture-manual.md.
  markdownDocRoute("docs/architecture-manual", {
    heading: "Architecture Manual",
    src: "/assets/docs/architecture-manual.md",
  }),
  // Served copy of docs/indicator-reliability-methodology.md.
  markdownDocRoute("docs/indicator-reliability-methodology", {
    heading: "Indicator Reliability — Methodology",
    src: "/assets/docs/indicator-reliability-methodology.md",
  }),
  // Served copy of docs/signal-engine-authority.md.
  markdownDocRoute("docs/signal-engine-methodology", {
    heading: "Signal Engine — Methodology",
    src: "/assets/docs/signal-engine-methodology.md",
  }),
  {
    path: "legal/notices",
    loadComponent: () =>
      import(
        "./components/legal/legal-notices-page/legal-notices-page.component"
      ).then((m) => m.LegalNoticesPageComponent),
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
    // Settings — the Configuration tab until PRD #2560 — belongs to one
    // account's lane (FR-092) and lives only under its clerk. A broker-wide
    // bookmark names no lane, so, like the retired Bots and Gallery
    // choosers below, it lands on the account list, where choosing the
    // account is choosing whose Settings to open — never a lane picked for
    // the operator (FR-096).
    path: "brokers/alpaca/settings",
    redirectTo: "/brokers/alpaca",
    pathMatch: "full",
  },
  {
    path: "brokers/alpaca/configuration",
    redirectTo: "/brokers/alpaca",
    pathMatch: "full",
  },
  {
    // ── The account workspace (ADR 0064 Decision 1, PRD §13/FR-092) ─────────
    // One account is one place: the account header and its tabs are this
    // parent, and each tab is a child, so moving between them never
    // re-creates the header or the shared account read. Home is the bare
    // account URL, and `:clerkId`/`:accountId` reach every child through the
    // router's `paramsInheritanceStrategy: 'always'` (see `app.config.ts`).
    //
    // The shell sits at the CLERK level, not the account level, because a
    // lane can be open without an account: Settings is lane-scoped
    // wherever it is opened from (FR-092), and a lane with no confirmed
    // account still keeps its workspace, with its Home explaining in place
    // why it cannot open (FR-096). `accounts/:accountId` is a componentless child
    // that adds account identity to the tabs that have one, so both cases
    // render under one header rather than two near-identical shells.
    //
    // Full-bleed on the PARENT, not on one tab: the header and the tab strip
    // are the workspace's own chrome and must sit at the same place on every
    // tab. Declaring it per tab gave the shell's page inset to some tabs and
    // not others, which moved the header ~24px when the operator switched
    // tabs. Each tab now owns whatever inset its own content wants.
    path: 'brokers/alpaca/clerks/:clerkId',
    data: { fullBleed: true, broker: 'alpaca' },
    // The Trader/Operator lens is retired (PRD #2560 D2): an old link's
    // `?lens=` is dropped, on entry and on every move inside the workspace.
    canActivate: [dropRetiredLensGuard],
    runGuardsAndResolvers: 'paramsOrQueryParamsChange',
    loadComponent: () =>
      import(
        './components/brokers/alpaca-workspace/alpaca-account-workspace.component'
      ).then((m) => m.AlpacaAccountWorkspaceComponent),
    children: [
      {
        // The lane's Settings — repair stays reachable without a confirmed
        // binding (configuration-access readiness), which is why it is the
        // one tab an unbound lane can still serve.
        path: 'settings',
        loadComponent: () =>
          import(
            './components/brokers/alpaca-desk/configuration/alpaca-settings-page.component'
          ).then((m) => m.AlpacaSettingsPageComponent),
      },
      {
        // History — every bot across every account (#2574). Lane-scoped like
        // Settings: it is the same list from every workspace and needs no
        // confirmed account. Its filters live in the query.
        path: 'history',
        loadComponent: () =>
          import(
            './components/brokers/alpaca-history/alpaca-history-page.component'
          ).then((m) => m.AlpacaHistoryPageComponent),
      },
      {
        // Settings was the Configuration tab until PRD #2560; the redirect
        // keeps a bookmark's `?profileId&revision` review request with it.
        path: 'configuration',
        redirectTo: 'settings',
        pathMatch: 'full',
      },
      {
        // The lane's Home when it has no confirmed account to serve one: the
        // in-place explanation of why (not ready, unbound, or without the
        // capability), plus its way to Settings. Never redirected to
        // another lane (FR-096).
        path: 'home',
        loadComponent: () =>
          import(
            './components/brokers/alpaca-workspace/alpaca-surface-not-ready-tab.component'
          ).then((m) => m.AlpacaSurfaceNotReadyTabComponent),
      },
      // Bots and Gallery merged into Home (PRD #2560); their lane-scoped
      // bookmarks land on the lane's Home.
      { path: 'bots', canActivate: [homeRedirectGuard('list')], children: [] },
      { path: 'gallery', canActivate: [homeRedirectGuard('wall')], children: [] },
      {
        // The account-scoped tabs. Componentless: it contributes account
        // identity to the URL, not a second shell under the first.
        path: 'accounts/:accountId',
        children: [
          {
            // One bot's own page — inside the workspace, under Home.
            path: 'bots/:sid',
            loadComponent: () =>
              import(
                './components/broker/v2-panel/panel-shell/bot-panel-shell.component'
              ).then((m) => m.BotPanelShellComponent),
          },
          // Overview, Bots and Gallery merged into Home (PRD #2560): the old
          // tabs' bookmarks land on it, Gallery as its Wall view.
          { path: 'bots', canActivate: [homeRedirectGuard('list')], children: [] },
          { path: 'gallery', canActivate: [homeRedirectGuard('wall')], children: [] },
          {
            // Activity — the account's history and records (PRD #2560):
            // Today / 30D / 60D money, fees per bot, orders and cash moves,
            // the sync check, and order records and recovery.
            path: 'activity',
            loadComponent: () =>
              import(
                './components/brokers/alpaca-desk/activity/alpaca-activity-page.component'
              ).then((m) => m.AlpacaActivityPageComponent),
          },
          {
            // Deploy — binds a validated strategy to this account, inline in
            // the tab strip rather than as an overlay drawer.
            path: 'deploy',
            loadComponent: () =>
              import(
                './components/brokers/alpaca-workspace/alpaca-deploy-tab.component'
              ).then((m) => m.AlpacaDeployTabComponent),
          },
          {
            // Home — the empty child, so the account's own URL opens it.
            path: '',
            loadComponent: () =>
              import('./components/brokers/alpaca-home/alpaca-home.component').then(
                (m) => m.AlpacaHomeComponent,
              ),
          },
        ],
      },
      {
        // A lane deep link without a tab: its Settings is the lane's own home
        // (the account list is the broker-level surface).
        path: '',
        redirectTo: 'settings',
        pathMatch: 'full',
      },
    ],
  },
  // The broker-wide Bots and Gallery choosers are retired (ADR 0064
  // Decision 2): a surface belongs to an account, so choosing one is
  // choosing an account, and the account list is where that happens. Both
  // bookmarks redirect there rather than 404-ing — including the old
  // `?surface=` hints, which the guard below still folds onto these two
  // paths and which therefore land on the list in one further hop.
  {
    path: "brokers/alpaca/bots",
    redirectTo: "/brokers/alpaca",
    pathMatch: "full",
  },
  {
    path: "brokers/alpaca/gallery",
    redirectTo: "/brokers/alpaca",
    pathMatch: "full",
  },
  {
    // The account list — the only multi-account page (ADR 0064 Decision 2).
    // The `?deploy` intent is this same route with a query param: the list is
    // the deploy entry point's account-selection step, never a lane picked
    // for the operator (FR-096).
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
    path: 'brokers/:broker/clerks/:clerkId/settings',
    loadComponent: loadBrokerLaneUnavailable,
  },
  {
    path: 'brokers/:broker/clerks/:clerkId/configuration',
    redirectTo: '/brokers/:broker/clerks/:clerkId/settings',
    pathMatch: 'full',
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
