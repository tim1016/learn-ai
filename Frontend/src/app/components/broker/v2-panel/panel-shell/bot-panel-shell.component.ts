import {
  ChangeDetectionStrategy,
  Component,
  DestroyRef,
  ElementRef,
  Injector,
  afterNextRender,
  computed,
  effect,
  inject,
  input,
  linkedSignal,
  resource,
  signal,
  viewChild,
} from '@angular/core';
import { toSignal } from '@angular/core/rxjs-interop';
import { ActivatedRoute, Router } from '@angular/router';
import { MessageService } from 'primeng/api';

import type {
  HistoricalExecutionRecoveryPlan,
  SqliteExtendedLimitConfirmation,
  SqliteExtendedLimitPricing,
  SqliteSafeFlattenPlan,
  SqliteSafeFlattenPricing,
} from '../../../../api/alpaca.types';
import {
  ExtendedFlattenTicketComponent,
  formatLimitPrice,
} from '../../shared/extended-flatten-ticket/extended-flatten-ticket.component';
import { FlattenFillsComponent } from '../../shared/flatten-fills/flatten-fills.component';
import { SafeFlattenPlanComponent } from '../../shared/safe-flatten-plan/safe-flatten-plan.component';
import { TypedHaltConfirmComponent } from '../../shared/typed-halt-confirm/typed-halt-confirm.component';
import type {
  ChartHistoryTimeframe,
  ChartLiveResolution,
  CurrentRunState,
  PanelAction,
  PanelActionResult,
  PanelActionTrigger,
} from '../lib/broker-v2-panel.types';
import { BrokerV2PanelService } from '../lib/broker-v2-panel.service';
import { BotPanelLiveStore } from '../lib/bot-panel-live-store.service';
import { TimestampDisplayComponent } from '../../../../shared/timestamp/timestamp-display.component';
import { BrokersService } from '../../../../services/brokers.service';
import {
  ORIGIN_TAB_QUERY_PARAM,
  accountWorkspaceOriginTab,
  accountWorkspaceOriginTabRoute,
  accountWorkspaceTabLabel,
} from '../../../../fleet/account-workspace';
import {
  laneKey,
  resourceTarget,
  type ResourceTarget,
  withCommand,
} from '../../../../fleet/resource-target';
import { accountWorkspaceTabRoute } from '../../../../fleet/account-workspace';
import { FleetDirectoryService } from '../../../../fleet/fleet-directory.service';
import {
  fencedTarget,
  freezeLaneFence,
  laneFenceVerdict,
  LANE_FENCE_REFRESH_FAILED_MESSAGE,
} from '../../../../fleet/lane-fence';
import { openLaneFence } from '../../../../fleet/open-lane-fence';
import { WorkspaceTitleContextService } from '../../../../shell/workspace-title-context.service';
import {
  actionOutcomeToast,
  deriveActionRejection,
  extractActionErrorDetail,
  type ActionRejection,
} from '../lib/panel-action-outcome';
import { BotBannerComponent } from '../bot-banner/bot-banner.component';
import { DeploymentBudgetComponent } from '../../deployment-budget/deployment-budget.component';
import { TradesTodayListComponent } from '../bot-page/trades-today-list.component';
import { RecentDecisionsListComponent } from '../bot-page/recent-decisions-list/recent-decisions-list.component';
import { BotDayChartComponent } from '../bot-page/bot-day-chart.component';
import { BotDetailsComponent } from '../bot-page/bot-details.component';
import { StrandedPositionWarningComponent } from '../bot-page/stranded-position-warning.component';
import {
  flattenUnderway,
  initialFlattenSteps,
  refreshedLimitTicket,
  runFlattenSequence,
  settleFlattenStep,
  type FlattenOutcome,
  type FlattenRequest,
  type FlattenStepId,
  type FlattenStepState,
  type FlattenStepView,
} from '../bot-page/flatten-sequence';
import {
  type ActionReceiptView,
  PanelActionReceiptComponent,
} from './panel-action-receipt.component';

/** One command's ownership of this page (#2471).
 *
 * Every command this shell sends — an ordinary action, a safe flatten's
 * prepare and send, an exact-execution recovery — captures the lane,
 * account and bot it was sent to at click time and answers through this one
 * rule: the outcome's receipt renders only while this page still shows that
 * bot, and a notification that lands after the page has moved on names the
 * bot it came from instead of speaking as whichever bot is on screen. */
interface ActionOwnership {
  /** The page still shows the lane, account and bot this command targeted. */
  stillOwns(): boolean;
  /** Render an outcome under its own bot; when the page has moved on, skip
   * the receipt and name the originating bot in the notification. */
  deliverOutcome(receipt: ActionReceiptView): void;
}

interface HistoricalExecutionRecoveryDraft {
  readonly action: PanelAction;
  readonly plan: HistoricalExecutionRecoveryPlan;
  /** The lane shown when the operator opened this confirmation. */
  readonly target: ResourceTarget;
  readonly sid: string;
}

/** A prepared safe flatten: the plan, how it would go out now, and what re-prices it. */
interface PreparedSafeFlatten {
  readonly plan: SqliteSafeFlattenPlan;
  readonly pricing: SqliteSafeFlattenPricing | null;
  /** When this browser received ``pricing`` — what the ticket ages the quote by. */
  readonly receivedAtMs: number;
  /** The lane shown when the operator prepared. */
  readonly target: ResourceTarget;
  readonly sid: string;
  /** Why the last quote refresh failed; the quote then ages until it cannot be sent. */
  readonly quoteError: string | null;
}

/** An extended-hours ticket re-reads the live quote this often while open (#2007). */
const EXTENDED_FLATTEN_QUOTE_REFRESH_MS = 2_000;

/** The commands of a safe flatten: from anywhere on the page, each opens the
 * stranded-position warning's one confirmation (H30). */
const SAFE_FLATTEN_ACTION_IDS: ReadonlySet<PanelAction['action_id']> = new Set([
  'prepare_safe_flatten',
  'execute_safe_flatten',
]);

/** Why a Flatten trigger opened no confirmation. */
const FLATTEN_NOT_OFFERED = {
  underway: 'A flatten is already under way on this page. Nothing more was sent.',
  elsewhere: 'Flatten is offered for a stopped bot holding one position, from the warning at the top of its page. '
    + 'Nothing was sent.',
} as const;

/** The command each flatten step sends, named on a refusal's receipt. */
const FLATTEN_STEP_ACTIONS: Readonly<Record<FlattenStepId, PanelAction['action_id']>> = {
  reconcile: 'reconcile_now',
  plan: 'prepare_safe_flatten',
  sell: 'execute_safe_flatten',
};

/**
 * The bot page (PRD #2560 D2): one view, no lens.
 *
 * Top to bottom: the header with the backend's one primary action; the last
 * action's outcome, which takes the keyboard when it lands (story 48); for a
 * stopped bot that still holds shares, the warning with Flatten beside it
 * (stories 44–45); the day's chart beside "This bot's money"; fills and
 * recent decisions; and the audit depth folded under Details (story 47).
 *
 * ## Shell responsibilities
 * - Route parameter extraction (broker, clerk, account, sid).
 * - Data loading: the live panel snapshot, the current run, the delayed
 *   history chart.
 * - Action execution, each command owned by the bot it was sent to (#2471),
 *   including the one-confirmation Flatten sequence (hurdle H30).
 */
@Component({
  selector: 'app-bot-panel-shell',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [
    ExtendedFlattenTicketComponent,
    FlattenFillsComponent,
    PanelActionReceiptComponent,
    SafeFlattenPlanComponent,
    TypedHaltConfirmComponent,
    BotBannerComponent,
    BotDayChartComponent,
    BotDetailsComponent,
    DeploymentBudgetComponent,
    RecentDecisionsListComponent,
    StrandedPositionWarningComponent,
    TimestampDisplayComponent,
    TradesTodayListComponent,
  ],
  templateUrl: './bot-panel-shell.component.html',
  styleUrl: './bot-panel-shell.component.scss',
  providers: [BotPanelLiveStore],
  host: {
    '[class.is-stale]': 'liveStall() !== null',
  },
})
export class BotPanelShellComponent {
  // ── Route inputs (Angular route input binding) ────────────────────────────

  readonly broker = input.required<string>();
  readonly clerkId = input.required<string>();
  readonly accountId = input.required<string>();
  readonly sid = input.required<string>();

  // ── Services ──────────────────────────────────────────────────────────────

  private readonly panelSvc = inject(BrokerV2PanelService);
  private readonly brokers = inject(BrokersService);
  private readonly liveStore = inject(BotPanelLiveStore);
  private readonly destroyRef = inject(DestroyRef);
  private readonly route = inject(ActivatedRoute);
  private readonly router = inject(Router);
  private readonly messageService = inject(MessageService);
  private readonly fleetDirectory = inject(FleetDirectoryService);
  private readonly titleContext = inject(WorkspaceTitleContextService);
  private readonly injector = inject(Injector);

  private readonly queryParams = toSignal(this.route.queryParamMap, {
    initialValue: this.route.snapshot.queryParamMap,
  });

  // ── The workspace tab this page belongs to ───────────────────────────────
  // A bot's page sits inside the account workspace, under the tab it was
  // opened from (ADR 0064 Decision 1): the Gallery tile and the roster's links
  // stamp that tab on the URL, the workspace's tab strip keeps it highlighted
  // from the same stamp, and the way back below returns there. A URL with no
  // stamp — pasted, bookmarked — belongs to Bots.

  private readonly originTab = computed(() =>
    accountWorkspaceOriginTab(this.queryParams().get(ORIGIN_TAB_QUERY_PARAM)),
  );

  protected readonly backRoute = computed(() =>
    accountWorkspaceOriginTabRoute(
      {
        broker: this.broker(),
        clerkId: this.clerkId(),
        accountId: this.accountId(),
      },
      this.originTab(),
    ),
  );

  protected readonly backLabel = computed(() => accountWorkspaceTabLabel(this.originTab()));

  /** The frozen lane context (FR-094): every request and command this shell
   * issues carries broker, clerk and account identity, and commands pin the
   * lane's binding generation from the rendered directory resource. */
  protected readonly target = computed(() => {
    const lane = this.fleetDirectory.lane(this.broker(), this.clerkId());
    return resourceTarget(this.broker(), this.clerkId(), {
      accountId: this.accountId(),
      entityId: this.sid(),
      bindingGeneration: lane?.effective_binding_generation ?? null,
      routingEpoch: lane?.routing_epoch ?? null,
    });
  });

  /** The fence the operator was shown. Captured when the shell renders the
   * lane and again only when the route identity changes; never at click time.
   * The backend can refuse only a generation we send, so re-reading it here
   * would make the fence structurally unable to fire (#2068). Wiring is the
   * shared `openLaneFence` helper — see its doc for why both the `untracked`
   * directory read and the eager materialization it performs are
   * load-bearing. */
  private readonly openFence = openLaneFence(
    () => freezeLaneFence(this.fleetDirectory.lane(this.broker(), this.clerkId())),
    () => `${this.broker()}::${this.clerkId()}`,
  );

  // ── Internal state ────────────────────────────────────────────────────────

  protected readonly selectedHistoryTimeframe = signal<ChartHistoryTimeframe>('1m');
  protected readonly liveResolution = signal<ChartLiveResolution>('5s');
  protected readonly actionPending = signal(false);
  /** Changes for route reuse and for a rebinding of the same visible lane. */
  /** The lane and bot this page is showing, as a value.
   *
   * A key, not the target object: `target()` rebuilds on any lane-directory
   * change, and comparing object identity made an in-flight request look like
   * a route change (it discarded a flatten the operator had just confirmed)
   * and reset every draft below on an ordinary refresh. */
  private readonly routeIdentity = computed(() => {
    const target = this.target();
    return `${laneKey(
      target.broker,
      target.clerkId,
      target.routingEpoch,
      target.bindingGeneration,
      target.accountId,
    )}::${this.sid()}`;
  });
  /** The last action outcome, owned by the lane, account and bot it was sent
   * to (#2471): moving the page to another bot, account or lane clears it,
   * exactly as the flatten and recovery drafts below already were. */
  protected readonly actionReceipt = linkedSignal({
    source: this.routeIdentity,
    computation: (): ActionReceiptView | null => null,
  });
  protected readonly preparedFlatten = linkedSignal({
    source: this.routeIdentity,
    computation: (): PreparedSafeFlatten | null => null,
  });
  protected readonly reductionPlan = computed(() => this.preparedFlatten()?.plan ?? null);
  protected readonly reductionPricing = computed(() => this.preparedFlatten()?.pricing ?? null);
  /** The priced extended-hours ticket, present only for a single-leg plan in PRE/POST. */
  protected readonly extendedFlattenTicket = computed<
    {
      readonly pricing: SqliteExtendedLimitPricing;
      readonly quantity: number;
      readonly receivedAtMs: number;
    } | null
  >(() => {
    const prepared = this.preparedFlatten();
    const pricing = prepared?.pricing;
    if (prepared === null || pricing?.kind !== 'extended_limit' || prepared.plan.legs.length !== 1) {
      return null;
    }
    return {
      pricing,
      quantity: prepared.plan.legs[0].quantity,
      receivedAtMs: prepared.receivedAtMs,
    };
  });
  /** Fills of an operator-priced flatten, with the slippage the Clerk measured (#2007). */
  protected readonly flattenFills = computed(() =>
    (this.panel()?.recent_fills ?? []).filter(
      (fill) => fill.slippage_bps !== null && fill.slippage_bps !== undefined,
    ),
  );
  private readonly extendedFlattenOpen = computed(() => this.extendedFlattenTicket() !== null);
  /** One quote refresh at a time: a slow check must not overlap the next tick. */
  private flattenQuoteInFlight = false;
  /** The in-flight refresh, so a Review can queue behind it instead of being dropped. */
  private flattenQuoteRun: Promise<void> = Promise.resolve();
  /** While a ticket is open the quote refreshes on its own, so what the
   * operator confirms is never older than one refresh — except while the
   * operator is reviewing a price, when a refresh would answer the Clerk's
   * reading of it with a quote that carries no reading at all. */
  private readonly extendedFlattenQuoteRefresh = effect((onCleanup) => {
    if (!this.extendedFlattenOpen()) return;
    const timer = setInterval(() => {
      if (this.flattenPriceCheck() === null) void this.refreshFlattenQuote();
    }, EXTENDED_FLATTEN_QUOTE_REFRESH_MS);
    onCleanup(() => clearInterval(timer));
  });
  /** The price the Clerk is being asked to read, while it is being asked. */
  private readonly flattenPriceCheck = signal<number | null>(null);
  protected readonly historicalRecoveryDraft = linkedSignal({
    source: this.routeIdentity,
    computation: (): HistoricalExecutionRecoveryDraft | null => null,
  });

  protected readonly panel = computed(() => this.liveStore.snapshot()?.panel ?? null);
  protected readonly liveChart = computed(() => {
    const chart = this.liveStore.snapshot()?.live_chart ?? null;
    return chart?.resolution === this.liveResolution() ? chart : null;
  });
  protected readonly liveStreamStatus = this.liveStore.status;
  /** The server's typed stale notice while the panel producer is stalled
   * (#2353). The frozen snapshot stays visible, dimmed and under the notice,
   * so its controls keep working: every action is re-checked by the server. */
  protected readonly liveStall = this.liveStore.stall;

  private readonly runLifecycle = computed(() => {
    const health = this.panel()?.health;
    return health === undefined ? null
      : `${health.running}:${health.duty_outcome?.recorded_at_ms ?? ''}`;
  });

  protected readonly currentRun = resource({
    params: () => this.runLifecycle() === null ? undefined : {
      target: this.target(), sid: this.sid(), lifecycle: this.runLifecycle(),
    },
    loader: ({ params }) => this.panelSvc.getCurrentRun(params.target, params.sid),
  });

  protected readonly currentRunState = computed<CurrentRunState>(() => ({
    run: this.currentRun.hasValue() ? this.currentRun.value() : null,
    loading: this.currentRun.isLoading(),
    failed: this.currentRun.error() !== undefined,
  }));

  protected readonly profile = resource({
    params: () => this.broker(),
    loader: ({ params }) => this.panelSvc.getPanelProfile(params),
  });

  /** The bot holds shares: a quantity, not money, so no dollar is read here. */
  protected readonly holdsShares = computed(() =>
    Object.values(this.panel()?.exposure ?? {}).some((quantity) => quantity !== 0),
  );

  /** A stopped bot that still holds shares has no one managing them (H31). */
  protected readonly stranded = computed(() => this.panel()?.health.running === false && this.holdsShares());

  /** The flatten sequence's steps on this bot, once the owner confirmed one.
   * Written only through `settleFlattenStep`. */
  protected readonly flattenSteps = linkedSignal({
    source: this.routeIdentity,
    computation: (): readonly FlattenStepView[] | null => null,
  });
  /** A step is still running — the sequence, or its Sell step waiting on the
   * extended-hours ticket — so no second flatten can start. */
  protected readonly flattenInProgress = computed(() => flattenUnderway(this.flattenSteps()));

  private readonly receiptView = viewChild(PanelActionReceiptComponent);
  private readonly strandedWarning = viewChild(StrandedPositionWarningComponent);
  private readonly flattenTicketView = viewChild<ElementRef<HTMLElement>>('flattenTicket');

  /** FR-006 (#2202): history read identity is broker + clerk + account + sid
   * + timeframe only. `ResourceTarget.bindingGeneration`/`routingEpoch` fence
   * commands, not reads — keying this on `this.target()` made a fleet
   * directory rebind or same-lane generation/epoch bump silently re-request
   * history underneath the user, even though nothing about what to read had
   * changed. */
  protected readonly histChart = resource({
    params: () => ({
      broker: this.broker(),
      clerkId: this.clerkId(),
      accountId: this.accountId(),
      sid: this.sid(),
      timeframe: this.selectedHistoryTimeframe(),
    }),
    loader: ({ params }) =>
      this.panelSvc.getHistoryChart(
        resourceTarget(params.broker, params.clerkId, { accountId: params.accountId }),
        params.sid,
        params.timeframe,
      ),
  });

  /** Settled-error presentation for the delayed pane (#2202 FR-002/FR-005): a
   * boolean the leaf components can render without ever touching the raw
   * `ResourceRef.error()`/`HttpErrorResponse`. */
  protected readonly histChartFailed = computed(() => this.histChart.error() !== undefined);

  protected readonly isLoaded = computed(
    () => this.panel() !== null && this.profile.hasValue(),
  );

  protected readonly loadError = computed(() => {
    const liveError = this.liveStore.error();
    if (liveError !== null) return liveError;
    const error = this.profile.error();
    if (error === undefined || error === null) return null;
    return error instanceof Error ? error.message : 'Failed to load panel data.';
  });

  // ── Lifecycle ─────────────────────────────────────────────────────────────

  constructor() {
    const runPollTimer = setInterval(() => {
      if (this.panel()?.health.running && !this.currentRun.isLoading()) {
        this.currentRun.reload();
      }
    }, 5_000);
    this.destroyRef.onDestroy(() => clearInterval(runPollTimer));
    effect(() => {
      const target = this.target();
      void this.liveStore.start({
        ...this.routeParams(),
        resolution: this.liveResolution(),
        bindingGeneration: target.bindingGeneration,
        routingEpoch: target.routingEpoch,
      });
    });
    // The window names a bot's page by the bot (ADR 0064 Decision 6), and the
    // bot's label is panel data the shell above does not read. This is the one
    // fact this page publishes upward; it is cleared on the way out so no
    // other page can inherit it.
    effect(() => this.titleContext.setBotLabel(this.panel()?.strategy_label ?? null));
    this.destroyRef.onDestroy(() => {
      this.titleContext.setBotLabel(null);
      this.liveStore.stop();
    });
  }

  private routeParams(): {
    broker: string;
    clerkId: string;
    accountId: string;
    sid: string;
  } {
    return {
      broker: this.broker(),
      clerkId: this.clerkId(),
      accountId: this.accountId(),
      sid: this.sid(),
    };
  }

  // ── Template handlers ─────────────────────────────────────────────────────

  /** Capture an action's ownership of this page at click time (#2471). */
  private beginActionOwnership(): ActionOwnership {
    const identity = this.routeIdentity();
    const botName = this.panel()?.strategy_label ?? this.sid();
    return {
      stillOwns: () => this.routeIdentity() === identity,
      deliverOutcome: (receipt) => {
        if (this.routeIdentity() !== identity) {
          // The page has moved to another bot: the receipt stays off the new
          // bot's panel, and the notification says which bot it came from.
          const toast = actionOutcomeToast(receipt.outcome, receipt.message, receipt.remediation);
          this.messageService.add({ ...toast, detail: `${botName}: ${toast.detail}` });
          return;
        }
        this.actionReceipt.set(receipt);
        this.messageService.add(actionOutcomeToast(receipt.outcome, receipt.message, receipt.remediation));
        // The outcome takes the keyboard once it is on screen (story 48).
        afterNextRender(() => this.receiptView()?.focus(), { injector: this.injector });
      },
    };
  }

  protected onHistoryTimeframeChange(timeframe: ChartHistoryTimeframe): void {
    this.selectedHistoryTimeframe.set(timeframe);
  }

  protected onLiveResolutionChange(resolution: ChartLiveResolution): void {
    this.liveResolution.set(resolution);
  }

  protected onTransactionSelected(transactionRef: string): void {
    void this.liveStore.selectTransaction(transactionRef);
  }

  protected dismissActionReceipt(): void {
    this.actionReceipt.set(null);
  }

  protected async onActionRequested({ action, reason }: PanelActionTrigger): Promise<void> {
    if (this.actionPending()) return;
    if (SAFE_FLATTEN_ACTION_IDS.has(action.action_id)) {
      this.offerFlatten(action);
      return;
    }
    // An action is bound to the rendered lane, not the reactive route. Capture
    // both before any branch can await or display a confirmation.
    const fence = this.openFence();
    const verdict = laneFenceVerdict(fence, this.fleetDirectory.lane(this.broker(), this.clerkId()));
    if (!verdict.ok) {
      this.actionReceipt.set(this.conflictReceipt(action, verdict.message));
      this.messageService.add(actionOutcomeToast('conflict', verdict.message));
      return;
    }
    const target = fencedTarget(this.target(), fence);
    const sid = this.sid();
    if (action.action_id === 'open_custody_timeline') {
      // Activity's "Order records and recovery" fold opens on arrival at
      // this bot's timeline (`sqliteTimelineQueryFromParams`).
      const activity = accountWorkspaceTabRoute({
        broker: target.broker, clerkId: target.clerkId, accountId: this.requiredAccountId(target),
      }, 'activity');
      if (activity !== null) {
        void this.router.navigate([...activity], { queryParams: this.custodyTimelineQuery(action, sid) });
      }
      return;
    }
    if (action.action_id === 'recover_exact_execution_evidence') {
      await this.prepareHistoricalExecutionRecovery(action, this.commandTarget(target), sid);
      return;
    }
    this.actionPending.set(true);
    this.actionReceipt.set(null);
    const ownership = this.beginActionOwnership();
    try {
      // runBotAction is resilient: a Stop-409 (transient token flip) is retried
      // once with a fresh token instead of dead-ending the operator (defect #10).
      const result = await this.panelSvc.runBotAction(
        this.commandTarget(target),
        sid,
        action,
        reason,
      );
      const receipt = this.successReceipt(result);
      ownership.deliverOutcome(receipt);
      await this.liveStore.refresh();
    } catch (error) {
      const rejection = this.describeRejection(error, action);
      const receipt = this.errorReceipt(error, action, rejection);
      ownership.deliverOutcome(receipt);
      // A stale-generation refusal means the fence the operator was shown is
      // provably wrong; refresh so the next action is minted against a lane
      // they have actually seen (#2068).
      if (rejection.reasonCode === 'clerk_binding_generation_conflict') {
        void this.fleetDirectory.refresh().catch(() => {
          this.messageService.add(actionOutcomeToast('failure', LANE_FENCE_REFRESH_FAILED_MESSAGE));
        });
      }
      // The rejection is always pre-execution (see runBotAction's doc), so the
      // operator's last-seen panel state is now stale relative to whatever
      // changed underneath it — refresh the current backend decision.
      //
      // No .catch() here, unlike fleetDirectory.refresh() above: BotPanelLiveStore.refresh()
      // catches internally and stores the failure as error state (it never rejects), while
      // FleetDirectoryService.refresh() deliberately does reject — this asymmetry is correct,
      // not an oversight.
      await this.liveStore.refresh();
    } finally {
      this.actionPending.set(false);
    }
  }

  /**
   * Flatten a stopped bot's stranded position on one confirmation (H30).
   *
   * The owner confirmed exactly this symbol and quantity; the sequence
   * reconciles, prepares, checks the plan sells precisely that, and sends it
   * — every step bound to the lane, account and bot the owner confirmed on.
   * Outside regular hours it stops at the verified plan and opens the
   * extended-hours ticket, where the owner sets the limit price.
   */
  protected async onFlattenConfirmed(request: FlattenRequest): Promise<void> {
    const panel = this.panel();
    if (this.actionPending() || this.flattenInProgress() || panel === null) return;
    const fence = this.openFence();
    const verdict = laneFenceVerdict(fence, this.fleetDirectory.lane(this.broker(), this.clerkId()));
    if (!verdict.ok) {
      this.beginActionOwnership().deliverOutcome({
        actionId: 'reconcile_now', outcome: 'conflict', receiptId: null,
        recordedAtMs: Date.now(), message: verdict.message, remediation: null,
      });
      return;
    }
    const target = fencedTarget(this.target(), fence);
    const sid = this.sid();
    const ownership = this.beginActionOwnership();
    this.actionPending.set(true);
    this.actionReceipt.set(null);
    this.preparedFlatten.set(null);
    this.flattenSteps.set(initialFlattenSteps(panel.mode));
    try {
      const outcome = await runFlattenSequence({
        readPanel: () => this.panelSvc.getPanel(target, sid),
        runAction: (action) => this.panelSvc.runBotAction(this.commandTarget(target), sid, action),
        checkPlan: (prepare) => this.brokers.checkSqliteSafeFlatten(
          target.clerkId,
          this.requiredAccountId(target),
          { action_id: 'prepare_safe_flatten', concurrency_token: prepare.concurrency_token },
          sid,
        ),
        report: (step, state, message) => {
          if (ownership.stillOwns()) this.settleFlattenStep(step, state, message);
        },
      }, request);
      this.finishFlatten(outcome, ownership, target, sid);
      await this.liveStore.refresh();
    } finally {
      this.actionPending.set(false);
    }
  }

  private finishFlatten(
    outcome: FlattenOutcome,
    ownership: ActionOwnership,
    target: ResourceTarget,
    sid: string,
  ): void {
    switch (outcome.kind) {
      case 'sold':
        ownership.deliverOutcome(this.successReceipt(outcome.result));
        return;
      case 'needs_limit':
        if (!ownership.stillOwns()) return;
        this.preparedFlatten.set({
          plan: outcome.plan, pricing: outcome.pricing, receivedAtMs: Date.now(), target, sid, quoteError: null,
        });
        afterNextRender(() => this.flattenTicketView()?.nativeElement.focus(), { injector: this.injector });
        return;
      case 'failed':
        ownership.deliverOutcome(this.refusalReceipt(FLATTEN_STEP_ACTIONS[outcome.step], outcome.rejection));
        if (outcome.rejection.reasonCode === 'clerk_binding_generation_conflict') {
          void this.fleetDirectory.refresh().catch(() => {
            this.messageService.add(actionOutcomeToast('failure', LANE_FENCE_REFRESH_FAILED_MESSAGE));
          });
        }
        return;
    }
  }

  /**
   * Every safe-flatten trigger on the page — the Checks fold, a blocker's
   * move, the header — opens the stranded-position warning's confirmation,
   * so a flatten always runs as the one confirmed sequence (H30). With no
   * position to offer, or a flatten already under way, nothing opens and the
   * owner is told why.
   */
  private offerFlatten(action: PanelAction): void {
    if (this.strandedWarning()?.openConfirm()) return;
    this.beginActionOwnership().deliverOutcome(this.conflictReceipt(
      action,
      this.flattenInProgress() ? FLATTEN_NOT_OFFERED.underway : FLATTEN_NOT_OFFERED.elsewhere,
    ));
  }

  /** The one writer of the flatten steps: the sequence reports through it,
   * and the extended-hours ticket's outcomes finish its Sell step. */
  private settleFlattenStep(step: FlattenStepId, state: FlattenStepState, message: string | null): void {
    this.flattenSteps.update((steps) => settleFlattenStep(steps, step, state, message));
  }

  /** The extended-hours ticket closed without a sale: its Sell step fails
   * with the Clerk's reason, and the owner hears it. */
  private endLimitTicket(rejection: ActionRejection): void {
    this.preparedFlatten.set(null);
    this.settleFlattenStep('sell', 'failed', rejection.message);
    this.beginActionOwnership().deliverOutcome(this.refusalReceipt('execute_safe_flatten', rejection));
  }

  /** A refused flatten step as the owner reads it: the backend's words, its
   * code, and when the next session opens if that is the reason. */
  private refusalReceipt(actionId: PanelAction['action_id'], rejection: ActionRejection): ActionReceiptView {
    return {
      actionId,
      outcome: rejection.outcome,
      receiptId: null,
      recordedAtMs: Date.now(),
      message: rejection.message,
      remediation: rejection.why,
      reasonCode: rejection.reasonCode,
      availableAtMs: rejection.availableAtMs ?? null,
    };
  }

  /**
   * Re-read the prepared plan and its live IBKR quote, quietly (#2007).
   *
   * The token is always the Prepare action the panel presents *now*: the
   * Clerk re-mints it every reconciliation pass (~15 s), so re-checking with
   * the one captured at Prepare would 409 within a pass and the ticket would
   * never see a fresh quote again. A token that has just rotated is not a
   * failure — the panel poll catches up — so it refreshes the panel and
   * retries once instead of shouting at the operator. Anything else is named
   * beside the ticket, and the quote then ages past the Clerk's bound and
   * cannot be sent.
   */
  protected async refreshFlattenQuote(
    retryOnStaleToken = true,
    proposedLimitPrice: number | null = null,
    bandOverride = false,
  ): Promise<void> {
    const prepared = this.preparedFlatten();
    const prepare = this.presentedAction('prepare_safe_flatten');
    if (prepared === null || this.actionPending() || this.flattenQuoteInFlight) return;
    if (prepare === undefined) {
      this.preparedFlatten.set({
        ...prepared,
        quoteError: 'This bot no longer presents Prepare safe flatten; refresh the panel.',
      });
      return;
    }
    this.flattenQuoteInFlight = true;
    this.flattenQuoteRun = (async () => {
      try {
        const check = await this.brokers.checkSqliteSafeFlatten(
          prepared.target.clerkId,
          this.requiredAccountId(prepared.target),
          {
            action_id: 'prepare_safe_flatten',
            concurrency_token: prepare.concurrency_token,
            ...(proposedLimitPrice === null ? {} : { proposed_limit_price: proposedLimitPrice }),
            band_override: bandOverride,
          },
          prepared.sid,
        );
        if (this.preparedFlatten() !== prepared) return;
        const refreshed = refreshedLimitTicket(check);
        if (refreshed.kind === 'ended') {
          this.endLimitTicket(refreshed.rejection);
          return;
        }
        this.preparedFlatten.set({
          ...prepared,
          plan: refreshed.plan,
          pricing: refreshed.pricing,
          receivedAtMs: Date.now(),
          quoteError: null,
        });
      } catch (error) {
        if (this.preparedFlatten() !== prepared) return;
        const rejection = this.describeRejection(error, prepare);
        if (rejection.reasonCode === 'stale_action_token' && retryOnStaleToken) {
          this.flattenQuoteInFlight = false;
          await this.liveStore.refresh();
          await this.refreshFlattenQuote(false, proposedLimitPrice, bandOverride);
          return;
        }
        this.preparedFlatten.set({ ...prepared, quoteError: rejection.message });
      } finally {
        this.flattenQuoteInFlight = false;
      }
    })();
    await this.flattenQuoteRun;
  }

  /**
   * Ask the Clerk what the operator's own price would do (#2007).
   *
   * The browser derives no execution or cost figure of its own, so Review is
   * a round trip: the Clerk reads the price against the quote it holds, and
   * the ticket confirms against that reading. The periodic quote refresh
   * stands down while this runs, so the answer is not immediately replaced by
   * a quote carrying no reading.
   */
  protected async priceExtendedFlatten(limitPrice: number, bandOverride = false): Promise<void> {
    this.flattenPriceCheck.set(limitPrice);
    try {
      await this.flattenQuoteRun;
      await this.refreshFlattenQuote(true, limitPrice, bandOverride);
    } finally {
      this.flattenPriceCheck.set(null);
    }
  }

  /**
   * Send the extended-hours flatten at exactly the limit the operator reviewed (#2007).
   *
   * The panel is re-read first so the execute token is the freshest one the
   * Clerk has minted; without that a flatten lands inside the window after a
   * reconciliation pass rotated the token and is refused for staleness, which
   * is the worst possible moment to make an operator click twice.
   */
  protected async sendExtendedFlatten(confirmation: SqliteExtendedLimitConfirmation): Promise<void> {
    const prepared = this.preparedFlatten();
    if (prepared === null || prepared.pricing?.kind !== 'extended_limit' || this.actionPending()) {
      return;
    }
    const ownership = this.beginActionOwnership();
    this.actionPending.set(true);
    this.actionReceipt.set(null);
    try {
      await this.liveStore.refresh();
      if (!ownership.stillOwns()) return;
      const execute = await this.currentExecuteSafeFlatten(prepared.target, prepared.sid);
      if (!ownership.stillOwns()) return;
      if (execute === undefined) {
        throw new Error('This bot no longer presents a safe flatten; refresh and prepare again.');
      }
      const result = await this.panelSvc.executeExtendedSafeFlatten(
        this.commandTarget(prepared.target),
        prepared.sid,
        execute.concurrency_token,
        confirmation,
      );
      this.preparedFlatten.set(null);
      const price = formatLimitPrice(confirmation.limit_price);
      // "Sent" is a claim about the broker, so it is only made on the broker's
      // own evidence. A durably accepted EXIT whose reducing order has not
      // reached Alpaca — a lookup outage, a transiently blocked REDUCE — is
      // pending, not sent, and saying otherwise would tell an operator their
      // exposure is on its way out when nothing has left (Codex review
      // 2026-09-19).
      const reachedBroker = result.orders.some((order) => order.broker_order_id !== null);
      const message = !result.applied
        ? 'This flatten had already been sent; the durable result was replayed.'
        : reachedBroker
          ? `Limit order sent at $${price}. It fills only at that price or better; await its `
            + 'fill before treating exposure as flat.'
          : `Flatten accepted at $${price}, but no order has reached the broker yet. `
            + 'The Clerk keeps trying; nothing is flat until the order exists and fills.';
      if (ownership.stillOwns()) this.settleFlattenStep('sell', 'done', message);
      ownership.deliverOutcome({
        actionId: 'execute_safe_flatten',
        outcome: 'success',
        receiptId: result.receipt_id,
        recordedAtMs: result.recorded_at_ms,
        message,
        remediation: null,
      });
      await this.liveStore.refresh();
    } catch (error) {
      const rejection = deriveActionRejection(error, 'Action "Execute safe flatten" failed.');
      if (ownership.stillOwns()) this.settleFlattenStep('sell', 'failed', rejection.message);
      ownership.deliverOutcome(this.refusalReceipt('execute_safe_flatten', rejection));
      await this.liveStore.refresh();
    } finally {
      this.actionPending.set(false);
    }
  }

  /** The named action as the panel presents it right now, with its current token. */
  private presentedAction(actionId: PanelAction['action_id']): PanelAction | undefined {
    return this.panel()?.actions.find((candidate) => candidate.action_id === actionId);
  }

  /** The execute token comes from a live read: while the live projection is
   * stalled (#2353) the snapshot's token is frozen, so it is read from the
   * panel endpoint, which does not go through the stalled producer. */
  private async currentExecuteSafeFlatten(
    target: ResourceTarget,
    sid: string,
  ): Promise<PanelAction | undefined> {
    if (this.liveStore.stall() === null) return this.presentedAction('execute_safe_flatten');
    const panel = await this.panelSvc.getPanel(target, sid);
    return panel.actions.find((candidate) => candidate.action_id === 'execute_safe_flatten');
  }

  private commandTarget(target: ResourceTarget): ResourceTarget {
    return withCommand(target, 'bot_action', crypto.randomUUID());
  }

  protected cancelHistoricalExecutionRecovery(): void {
    this.historicalRecoveryDraft.set(null);
  }

  protected historicalRecoveryMessage(plan: HistoricalExecutionRecoveryPlan): string {
    return `Alpaca paper activity ${plan.execution_id} records ${plan.exact_side} ${plan.exact_quantity} at ${plan.exact_price}. It exactly matches cumulative recovery fill ${plan.cumulative_fill_id}.`;
  }

  protected async confirmHistoricalExecutionRecovery(): Promise<void> {
    const draft = this.historicalRecoveryDraft();
    if (draft === null || this.actionPending()) return;
    const ownership = this.beginActionOwnership();
    this.actionPending.set(true);
    this.actionReceipt.set(null);
    try {
      const receipt = await this.panelSvc.confirmHistoricalExecutionRecovery(
        draft.target,
        draft.sid,
        draft.plan,
      );
      this.historicalRecoveryDraft.set(null);
      const message = receipt.applied
        ? `${draft.action.label} completed. The Clerk recorded exact evidence without changing economic totals.`
        : `${draft.action.label} had already completed; the durable result was replayed.`;
      ownership.deliverOutcome({
        actionId: draft.action.action_id,
        outcome: 'success',
        receiptId: receipt.receipt_id,
        recordedAtMs: receipt.recorded_at_ms,
        message,
        remediation: null,
      });
      await this.liveStore.refresh();
    } catch (error) {
      this.historicalRecoveryDraft.set(null);
      ownership.deliverOutcome(this.errorReceipt(error, draft.action));
      await this.liveStore.refresh();
    } finally {
      this.actionPending.set(false);
    }
  }

  private async prepareHistoricalExecutionRecovery(
    action: PanelAction,
    target: ResourceTarget,
    sid: string,
  ): Promise<void> {
    const ownership = this.beginActionOwnership();
    this.actionPending.set(true);
    this.actionReceipt.set(null);
    this.historicalRecoveryDraft.set(null);
    try {
      const plan = await this.panelSvc.prepareHistoricalExecutionRecovery(
        target,
        sid,
        action.concurrency_token,
      );
      if (!ownership.stillOwns()) return;
      this.historicalRecoveryDraft.set({ action, plan, target, sid });
    } catch (error) {
      ownership.deliverOutcome(this.errorReceipt(error, action));
      await this.liveStore.refresh();
    } finally {
      this.actionPending.set(false);
    }
  }

  private custodyTimelineQuery(action: PanelAction, sid: string): Record<string, string> {
    const query: Record<string, string> = { timelineBot: sid };
    for (const reference of action.evidence_refs ?? []) {
      const separator = reference.indexOf(':');
      if (separator < 1 || separator === reference.length - 1) continue;
      const value = reference.slice(separator + 1);
      switch (reference.slice(0, separator)) {
        case 'order':
          return { ...query, timelineOrderRef: value };
        case 'execution':
          return { ...query, timelineExecutionId: value };
        case 'uncertainty':
          return { ...query, timelineUncertaintyId: value };
        case 'operation':
          return { ...query, timelineOperationRef: value };
      }
    }
    return query;
  }

  private successReceipt(result: PanelActionResult): ActionReceiptView {
    return {
      actionId: result.action_id,
      outcome: 'success',
      receiptId: result.receipt_id,
      recordedAtMs: result.recorded_at_ms,
      message: result.message,
      remediation: null,
    };
  }

  private requiredAccountId(target: ResourceTarget): string {
    if (target.accountId === null) {
      throw new Error('This action requires the account that was shown to the operator.');
    }
    return target.accountId;
  }

  private conflictReceipt(action: PanelAction, message: string): ActionReceiptView {
    return {
      actionId: action.action_id,
      outcome: 'conflict',
      receiptId: null,
      recordedAtMs: Date.now(),
      message,
      remediation: null,
    };
  }

  /** The one place the fallback failure message is built, so a caller that
   * needs the rejection ahead of the receipt (to branch on `reasonCode`)
   * derives it the same way `errorReceipt` would have derived it itself. */
  private describeRejection(error: unknown, action: PanelAction): ActionRejection {
    return deriveActionRejection(error, `Action "${action.label}" failed.`);
  }

  private errorReceipt(
    error: unknown,
    action: PanelAction,
    rejection: ActionRejection = this.describeRejection(error, action),
  ): ActionReceiptView {
    const detail = extractActionErrorDetail(error);
    return {
      actionId:
        typeof detail?.['action_id'] === 'string'
          ? detail['action_id']
          : action.action_id,
      outcome: rejection.outcome,
      receiptId:
        typeof detail?.['receipt_id'] === 'string' ? detail['receipt_id'] : null,
      recordedAtMs:
        typeof detail?.['recorded_at_ms'] === 'number'
          ? detail['recorded_at_ms']
          : Date.now(),
      message: rejection.message,
      remediation: rejection.why,
    };
  }
}
