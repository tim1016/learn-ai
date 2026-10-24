import { Routes, type Route } from "@angular/router";
import { alpacaSurfaceRedirectGuard } from "./fleet/alpaca-surface-redirect.guard";
import { homeRedirect } from "./fleet/home-redirect";
import { RETIRED_LENS_QUERY_PARAM, dropRetiredLensGuard } from "./fleet/retired-lens.guard";
import { EntryComponent } from "./components/entry/entry.component";
import { redirectLegacyDataLab } from "./components/data-lab/data-lab-ingress";
import { brokerClerkRedirectGuard } from "./fleet/broker-clerk-redirect.guard";
import { redirectKeepingState } from "./shell/route-redirects";

const loadBrokerLaneUnavailable = () =>
  import('./fleet/broker-lane-unavailable.component').then(
    (module) => module.BrokerLaneUnavailableComponent,
  );

// A served document copies a canonical repo document;
// `PythonDataService/tests/contracts/test_served_document_copies.py` fails CI
// when the two differ.
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
// older engine/runs/:id bookmark). Both redirect straight to this same final
// destination rather than to each other: a redirect lands in one hop (PRD
// #3490 R32). Angular follows an absolute redirect (a `/` string or a
// `UrlTree`, as here) into another redirect by restarting from the root; it
// does not follow a relative one, which is re-matched with redirects off and
// falls through to the wildcard. One hop is the one shape that works for both.
// The run id joins whatever else the bookmark carried (a tab, a filter, a
// fragment) rather than replacing it.
const redirectToStrategyLabRun = redirectKeepingState(["/strategy-lab"], {
  set: (params) => ({ run: String(params["id"] ?? "") }),
});

// The retired broker-wide choosers (Bots, Gallery, Settings and
// Configuration) end at the account list. Their query and fragment go with
// them, so a co-traveling `deploy` intent, the `strategy` it names and a
// `golden_qualification` handoff reach the list; the retired `?lens=` and
// `?surface=` hints do not.
const redirectToAccountList = redirectKeepingState(["/brokers", "alpaca"], {
  drop: [RETIRED_LENS_QUERY_PARAM, "surface"],
});

// A lane's bare address and its retired Configuration tab end at the lane's
// Settings. The retired `?lens=` is spent in this one redirect, so the old link
// does not reach the workspace's lens guard and move a second time; a review
// request (`?profileId&revision`) goes with it.
const redirectToLaneSettings = redirectKeepingState(
  (params) => ["/brokers", "alpaca", "clerks", String(params["clerkId"] ?? ""), "settings"],
  { drop: [RETIRED_LENS_QUERY_PARAM] },
);

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
  {
    // The app opens on the entry painting, whose "I'm In" leads to Data Lab.
    // There is no sign-in behind it: every other address still opens directly.
    // Loaded with the app, not lazily: the shell learns a page is chromeless
    // only once its route is active, so a lazy chunk would leave the top bar
    // on screen over the root address until it arrived.
    path: "",
    pathMatch: "full",
    component: EntryComponent,
    data: { fullBleed: true, chromeless: true },
  },
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
            "./components/options-lab/strategy-finder-unavailable/strategy-finder-unavailable.component"
          ).then((m) => m.StrategyFinderUnavailableComponent),
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
    // components/data-lab/data-lab.routes.ts.
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
    // Legacy /data-quality bookmark → the Validate child route (or the child
    // its `mode` names). Query and fragment go with it in the same
    // navigation; the Data Lab shell applies that query after landing.
    path: "data-quality",
    redirectTo: redirectLegacyDataLab("/data-lab/validate"),
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
    path: "golden-search",
    loadComponent: () =>
      import("./components/golden-search/golden-search-page.component").then(
        (m) => m.GoldenSearchPageComponent
      ),
  },
  {
    path: "golden-search/:studyId",
    loadComponent: () =>
      import("./components/golden-search/golden-search-page.component").then(
        (m) => m.GoldenSearchPageComponent
      ),
  },
  {
    path: "options-evidence/:kind/:subjectId",
    loadComponent: () => import("./components/options-evidence/options-evidence-page.component").then((m) => m.OptionsEvidencePageComponent),
  },
  {
    path: "options-golden-search",
    loadComponent: () => import("./components/options-golden-search/options-golden-search-page.component").then((m) => m.OptionsGoldenSearchPageComponent),
  },
  {
    path: "options-golden-search/:studyId",
    loadComponent: () => import("./components/options-golden-search/options-golden-search-page.component").then((m) => m.OptionsGoldenSearchPageComponent),
  },
  {
    path: "options-backtest",
    loadComponent: () =>
      import("./components/options-backtest/options-backtest-page.component").then(
        (m) => m.OptionsBacktestPageComponent
      ),
    // The run rail stays in view beside the form while the page scrolls.
    data: { stickyContent: true },
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
    // for the same reason as that route: a redirect lands in one hop.
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
    // Research: the study list, one page per study and the Edge demonstrations.
    path: "research",
    loadChildren: () =>
      import("./research/research.routes").then((m) => m.RESEARCH_ROUTES),
  },
  {
    // What Research Lab still hosts until slices 28 and 29 move it; its five
    // studies moved to /research and redirect from here.
    path: "research-lab",
    loadChildren: () =>
      import("./components/research-lab/research-lab.routes").then(
        (m) => m.researchLabRoutes
      ),
  },
  {
    path: "docs/options-backtest-manual",
    loadComponent: () => import("./components/docs/trader-manual/trader-manual.component").then(m => m.TraderManualComponent),
    data: { manual: 'options', stickyContent: true },
  },
  {
    path: "docs/broker-manual",
    loadComponent: () => import("./components/docs/trader-manual/trader-manual.component").then(m => m.TraderManualComponent),
    data: { manual: 'broker', stickyContent: true },
  },
  {
    path: "docs/bot-business-manual",
    loadComponent: () =>
      import("./components/docs/bot-manual/bot-manual.component").then(
        (m) => m.BotManualComponent,
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
  // In-app only: the guide lives with the served documents and has no repo copy.
  {
    path: "docs/golden-search-manual",
    loadComponent: () => import("./components/golden-search/manual/golden-search-manual.component").then(m => m.GoldenSearchManualComponent),
  },
  markdownDocRoute("docs/golden-search-guide", {
    heading: "Reading the Golden Search charts",
    src: "/assets/docs/golden-search-guide.md",
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
    redirectTo: redirectToAccountList,
    pathMatch: "full",
  },
  {
    path: "brokers/alpaca/configuration",
    redirectTo: redirectToAccountList,
    pathMatch: "full",
  },
  {
    // ── The account workspace (ADR 0064 Decision 1, FR-092) ─────────────────
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
        redirectTo: redirectToLaneSettings,
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
      { path: 'bots', redirectTo: homeRedirect('list'), pathMatch: 'full' },
      { path: 'gallery', redirectTo: homeRedirect('wall'), pathMatch: 'full' },
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
          { path: 'bots', redirectTo: homeRedirect('list'), pathMatch: 'full' },
          { path: 'gallery', redirectTo: homeRedirect('wall'), pathMatch: 'full' },
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
        redirectTo: redirectToLaneSettings,
        pathMatch: 'full',
      },
    ],
  },
  // The broker-wide Bots and Gallery choosers are retired (ADR 0064
  // Decision 2): a surface belongs to an account, so choosing one is
  // choosing an account, and the account list is where that happens. Both
  // bookmarks redirect there rather than 404-ing — including the old
  // `?surface=` hints, which the guard below sends to the list directly.
  {
    path: "brokers/alpaca/bots",
    redirectTo: redirectToAccountList,
    pathMatch: "full",
  },
  {
    path: "brokers/alpaca/gallery",
    redirectTo: redirectToAccountList,
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
  // Edge became Research: its Realized vs IV page is a study, and the rest are
  // demonstrations on sample data. Each old address goes straight to its page,
  // keeping its query and fragment; the hub, which showed hard-coded numbers,
  // goes to the study list that now names those pages.
  {
    path: "edge",
    redirectTo: redirectKeepingState(["/research"]),
    pathMatch: "full",
  },
  {
    path: "edge/realized-vs-iv",
    redirectTo: redirectKeepingState(["/research", "realized-vs-iv"]),
    pathMatch: "full",
  },
  {
    path: "edge/cross-asset",
    redirectTo: redirectKeepingState(["/research", "demos", "cross-asset"]),
    pathMatch: "full",
  },
  {
    path: "edge/regimes",
    redirectTo: redirectKeepingState(["/research", "demos", "regimes"]),
    pathMatch: "full",
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
    // Keeps `?profileId&revision` like the Alpaca twin does.
    path: 'brokers/:broker/clerks/:clerkId/configuration',
    redirectTo: redirectKeepingState((params) => [
      '/brokers',
      String(params['broker'] ?? ''),
      'clerks',
      String(params['clerkId'] ?? ''),
      'settings',
    ]),
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
  {
    // An address no page answers fails in place, at that address, with a way
    // back (PRD #3490 R01) — never a second redirect onto a default page that
    // drops the query and the fragment.
    path: "**",
    data: { notFound: true },
    loadComponent: () =>
      import("./shell/not-found.component").then((m) => m.NotFoundComponent),
  },
];
