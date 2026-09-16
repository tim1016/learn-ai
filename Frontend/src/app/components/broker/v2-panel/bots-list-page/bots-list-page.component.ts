import {
  ChangeDetectionStrategy,
  Component,
  DestroyRef,
  Injector,
  afterNextRender,
  computed,
  inject,
  input,
  resource,
  signal,
} from '@angular/core';
import { DOCUMENT } from '@angular/common';
import { RouterLink } from '@angular/router';
import { MessageService } from 'primeng/api';

import type { BrokerAccountSnapshot, ClerkStatus } from '../../../../api/alpaca.types';
import { BrokersService } from '../../../../services/brokers.service';
import { fmtElapsedSince } from '../../format';
import { AlpacaDeployDrawerComponent } from '../../broker-deploy-page/alpaca-deploy-drawer.component';
import { CohortArchiveDrawerComponent } from '../cohort-archive/cohort-archive-drawer.component';
import { AccountStripComponent } from '../account-strip/account-strip.component';
import { BotTriageDetailComponent } from '../bot-triage-detail/bot-triage-detail.component';
import {
  BotsRosterComponent,
  type RosterRowActionEvent,
} from '../bots-roster/bots-roster.component';
import { LaneContextStripComponent } from '../lane-context-strip/lane-context-strip.component';
import { BrokerV2PanelService } from '../lib/broker-v2-panel.service';
import { resourceTarget, withCommand, withEntity } from '../../../../fleet/resource-target';
import { FleetDirectoryService } from '../../../../fleet/fleet-directory.service';
import {
  fencedTarget,
  freezeLaneFence,
  laneFenceVerdict,
  LANE_FENCE_REFRESH_FAILED_MESSAGE,
} from '../../../../fleet/lane-fence';
import { openLaneFence } from '../../../../fleet/open-lane-fence';
import type { BotCatalogView, PanelActionTrigger } from '../lib/broker-v2-panel.types';
import { actionOutcomeToast, deriveActionRejection } from '../lib/panel-action-outcome';

const CATALOG_POLL_MS = 5_000;
const ACCOUNT_POLL_MS = 15_000;
/**
 * How old the fleet snapshot must be before the roster says so (#1806 item 3).
 *
 * Six poll cadences. The account strip's refresh *pills* fire on a failure
 * edge -- they answer "did a refresh just fail?". This banner answers "is what
 * I am looking at stale?", which is a state, so it is gated on the snapshot's
 * real age: a single failed poll the next poll repairs was never meaningfully
 * stale and stays silent.
 *
 * Deliberately well above the measured catalog read cost at fleet scale
 * (p95 20.6s at 144 rows, #1801) so a slow-but-landing read is not reported
 * as a failure. This is a staleness floor, not a latency budget -- #1801 owns
 * the latency curve and this must not be tuned as a proxy for it.
 */
const FLEET_STALE_AFTER_MS = 6 * CATALOG_POLL_MS;

interface ScopedSnapshot<T> {
  readonly scope: string;
  readonly updatedAtMs: number;
  readonly value: T;
}

@Component({
  selector: 'app-bots-list-page',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [
    AccountStripComponent,
    AlpacaDeployDrawerComponent,
    CohortArchiveDrawerComponent,
    BotTriageDetailComponent,
    BotsRosterComponent,
    LaneContextStripComponent,
    RouterLink,
  ],
  templateUrl: './bots-list-page.component.html',
  styleUrl: './bots-list-page.component.scss',
  host: { class: 'block h-full' },
})
export class BotsListPageComponent {
  readonly broker = input('alpaca');
  readonly clerkId = input.required<string>();
  readonly accountId = input.required<string>();

  private readonly brokersService = inject(BrokersService);
  private readonly panelService = inject(BrokerV2PanelService);
  private readonly fleetDirectory = inject(FleetDirectoryService);
  private readonly destroyRef = inject(DestroyRef);
  private readonly injector = inject(Injector);
  private readonly document = inject(DOCUMENT);
  private readonly messageService = inject(MessageService);
  private readonly catalogSnapshot = signal<ScopedSnapshot<BotCatalogView[]> | null>(null);
  private readonly accountSnapshot = signal<ScopedSnapshot<BrokerAccountSnapshot> | null>(null);
  private readonly clerkSnapshot = signal<ScopedSnapshot<ClerkStatus> | null>(null);

  protected readonly actionNotice = signal<{ tone: 'success' | 'danger'; message: string } | null>(
    null,
  );
  protected readonly pendingBotIds = signal<ReadonlySet<string>>(new Set());
  protected readonly deployOpen = signal(false);
  protected readonly archiveOpen = signal(false);
  private readonly requestedSid = signal<string | null>(null);
  /** Bumped after an action lands so the detail pane refetches its panel. */
  protected readonly detailRefreshToken = signal(0);

  /** Frozen lane context for every roster request and command. */
  protected readonly target = computed(() =>
    resourceTarget(this.broker(), this.clerkId(), {
      accountId: this.accountId(),
      bindingGeneration:
        this.fleetDirectory.lane(this.broker(), this.clerkId())
          ?.effective_binding_generation ?? null,
      routingEpoch: this.fleetDirectory.lane(this.broker(), this.clerkId())?.routing_epoch ?? null,
    }),
  );

  /** The routed lane itself, for the header's lane-context strip: its pill
   * (the same trust anchor the shell renders) plus its authority fact, so the
   * operator always sees which lane and which account this roster serves. */
  protected readonly routedLane = computed(
    () => this.fleetDirectory.lane(this.broker(), this.clerkId()) ?? null,
  );

  /** The fence the operator was shown. Captured when the roster renders the
   * lane and again only when the route identity changes; never at click time
   * (#2068). Wiring is the shared `openLaneFence` helper — see its doc for
   * why both the `untracked` directory read and the eager materialization
   * it performs are load-bearing. */
  private readonly openFence = openLaneFence(
    () => freezeLaneFence(this.fleetDirectory.lane(this.broker(), this.clerkId())),
    () => `${this.broker()}::${this.clerkId()}`,
  );

  /**
   * Values may only be rendered for the exact routed lane that produced them.
   * Account ids are provider-local, so an account id alone cannot distinguish
   * two Clerks (nor can it distinguish a binding replacement for one Clerk).
   */
  private readonly fleetScope = computed(() => {
    const target = this.target();
    return [
      target.broker,
      target.clerkId,
      target.accountId ?? '',
      target.bindingGeneration ?? '',
      target.routingEpoch ?? '',
    ].join(':');
  });

  protected readonly catalog = resource({
    params: () => ({ target: this.target(), scope: this.fleetScope() }),
    loader: async ({ params }) => {
      const startedAt = this.performanceNow();
      const bots = await this.panelService.getCatalog(params.target);
      const scope = params.scope;
      if (scope !== this.fleetScope()) return bots;
      const firstUsefulPaint = this.catalogSnapshot()?.scope !== scope;
      this.catalogSnapshot.set({
        scope,
        updatedAtMs: Date.now(),
        value: bots,
      });
      if (firstUsefulPaint) {
        this.measureAfterPaint('alpaca-bots-first-useful-roster-paint', startedAt);
      }
      this.measureAfterPaint('alpaca-bots-fresh-roster-paint', startedAt);
      return bots;
    },
  });

  protected readonly account = resource({
    params: () => ({
      broker: this.broker(),
      clerkId: this.clerkId(),
      accountId: this.accountId(),
      target: this.target(),
      scope: this.fleetScope(),
    }),
    loader: async ({ params }) => {
      const snapshot = await this.brokersService.getAccount(params.target);
      if (params.scope !== this.fleetScope()) return snapshot;
      if (snapshot.account_id !== params.accountId) {
        throw new Error(
          `Alpaca confirmed account ${snapshot.account_id}, not routed account ${params.accountId}.`,
        );
      }
      this.accountSnapshot.set({
        scope: params.scope,
        updatedAtMs: snapshot.observed_at_ms,
        value: snapshot,
      });
      return snapshot;
    },
  });

  protected readonly clerkStatus = resource({
    params: () => ({
      broker: this.broker(),
      clerkId: this.clerkId(),
      accountId: this.accountId(),
      target: this.target(),
      scope: this.fleetScope(),
    }),
    loader: async ({ params }) => {
      const snapshot = await this.brokersService.getClerkStatus(params.target);
      if (params.scope !== this.fleetScope()) return snapshot;
      if (snapshot.account_id !== params.accountId) {
        throw new Error(
          `The Clerk is observing account ${snapshot.account_id}, not routed account ${params.accountId}.`,
        );
      }
      this.clerkSnapshot.set({
        scope: params.scope,
        updatedAtMs: snapshot.observed_at_ms,
        value: snapshot,
      });
      return snapshot;
    },
  });

  protected readonly bots = computed(() => {
    const snapshot = this.catalogSnapshot();
    return snapshot?.scope === this.fleetScope() ? snapshot.value : [];
  });
  protected readonly accountValue = computed(() => {
    const snapshot = this.accountSnapshot();
    return snapshot?.scope === this.fleetScope() ? snapshot.value : null;
  });
  protected readonly clerkValue = computed(() => {
    const snapshot = this.clerkSnapshot();
    return snapshot?.scope === this.fleetScope() ? snapshot.value : null;
  });
  protected readonly catalogUpdatedAtMs = computed(() => {
    const snapshot = this.catalogSnapshot();
    return snapshot?.scope === this.fleetScope() ? snapshot.updatedAtMs : null;
  });
  /**
   * The bot the detail pane shows. Derived rather than stored so an operator's
   * choice survives a poll, but a bot that leaves the fleet (or an account
   * switch) falls back to the most urgent row instead of stranding the pane on
   * a bot that no longer exists.
   */
  protected readonly selectedSid = computed<string | null>(() => {
    const bots = this.bots();
    if (bots.length === 0) return null;
    const requested = this.requestedSid();
    if (requested !== null && bots.some((bot) => bot.strategy_instance_id === requested)) {
      return requested;
    }
    const attention = bots.find((bot) => bot.needs_attention);
    return (attention ?? bots[0]).strategy_instance_id;
  });

  protected readonly selectionPending = computed(() => {
    const sid = this.selectedSid();
    return sid !== null && this.pendingBotIds().has(sid);
  });

  protected readonly initialLoading = computed(
    () => this.catalog.isLoading() && this.bots().length === 0,
  );
  protected readonly refreshing = computed(
    () => this.catalog.isLoading() && this.bots().length > 0,
  );
  protected readonly postureLoading = computed(
    () =>
      (this.account.isLoading() || this.clerkStatus.isLoading()) &&
      !this.accountValue(),
  );
  protected readonly postureRefreshing = computed(
    () =>
      (this.account.isLoading() || this.clerkStatus.isLoading()) &&
      Boolean(this.accountValue()),
  );
  protected readonly unavailable = computed(
    () => Boolean(this.catalog.error()) && this.bots().length === 0,
  );
  /** Ticks so the rendered snapshot age stays true without a refetch. */
  private readonly nowMs = signal(Date.now());
  protected readonly stale = computed(() => {
    const updatedAtMs = this.catalogUpdatedAtMs();
    if (updatedAtMs === null || this.bots().length === 0) return false;
    return this.nowMs() - updatedAtMs >= FLEET_STALE_AFTER_MS;
  });
  /** Quantifies the staleness the banner claims, via the shared formatter. */
  protected readonly staleAge = computed(() => {
    const updatedAtMs = this.catalogUpdatedAtMs();
    return updatedAtMs === null ? '' : fmtElapsedSince(updatedAtMs, this.nowMs());
  });
  constructor() {
    afterNextRender(() => this.mark('alpaca-bots-route-shell'));

    const catalogTimer = setInterval(() => {
      if (this.document.visibilityState === 'visible' && !this.catalog.isLoading()) {
        this.catalog.reload();
      }
    }, CATALOG_POLL_MS);
    const staleTimer = setInterval(() => {
      if (this.document.visibilityState === 'visible') {
        this.nowMs.set(Date.now());
      }
    }, 1_000);

    const accountTimer = setInterval(() => {
      if (
        this.document.visibilityState === 'visible' &&
        !this.account.isLoading() &&
        !this.clerkStatus.isLoading()
      ) {
        this.account.reload();
        this.clerkStatus.reload();
      }
    }, ACCOUNT_POLL_MS);
    this.destroyRef.onDestroy(() => {
      clearInterval(catalogTimer);
      clearInterval(staleTimer);
      clearInterval(accountTimer);
    });
  }

  protected refreshFleet(): void {
    this.actionNotice.set(null);
    this.account.reload();
    this.clerkStatus.reload();
    this.catalog.reload();
  }

  protected openDeploy(): void {
    this.deployOpen.set(true);
  }

  protected closeDeploy(): void {
    this.deployOpen.set(false);
  }

  protected openArchive(): void {
    this.archiveOpen.set(true);
  }

  protected closeArchive(): void {
    this.archiveOpen.set(false);
  }

  protected selectBot(sid: string): void {
    this.requestedSid.set(sid);
    this.actionNotice.set(null);
  }

  /**
   * The single action-execution owner for this route. The detail pane presents
   * backend-declared actions (with their confirmations and blockers) and
   * delegates here, so every action on this screen shares one policy, one
   * pending set, and one toast path.
   */
  protected async onPanelAction(trigger: PanelActionTrigger): Promise<void> {
    const sid = this.selectedSid();
    if (sid === null) return;
    await this.runAction(sid, trigger);
  }

  /**
   * A roster row commands its own bot, which is not necessarily the selected
   * one. It still runs through the single execution owner above — same
   * pending set, same toast path, same post-action refresh policy.
   */
  protected async onRowAction({ bot, action }: RosterRowActionEvent): Promise<void> {
    await this.runAction(bot.strategy_instance_id, { action, reason: null });
  }

  private async runAction(sid: string, trigger: PanelActionTrigger): Promise<void> {
    if (this.pendingBotIds().has(sid)) return;

    const action = trigger.action;
    // Freeze the lane before any await. A roster action may outlive a route
    // reuse or a binding replacement; it must conflict rather than following
    // the operator to whatever lane happens to be current at submission time.
    const fence = this.openFence();
    const verdict = laneFenceVerdict(fence, this.fleetDirectory.lane(this.broker(), this.clerkId()));
    if (!verdict.ok) {
      this.actionNotice.set({ tone: 'danger', message: verdict.message });
      this.messageService.add(actionOutcomeToast('conflict', verdict.message));
      return;
    }
    const laneTarget = withEntity(fencedTarget(this.target(), fence), sid);
    const target = withCommand(laneTarget, 'bot_action', crypto.randomUUID());
    const scope = this.fleetScope();
    const startedAt = this.performanceNow();
    this.actionNotice.set(null);
    this.pendingBotIds.update((current) => new Set([...current, sid]));

    try {
      if (!action.enabled) {
        const message = `${action.label} is no longer available for ${sid}. Refreshing its current state.`;
        this.actionNotice.set({ tone: 'danger', message });
        this.messageService.add(actionOutcomeToast('conflict', message));
        return;
      }

      const result = await this.panelService.runBotAction(
        target,
        sid,
        action,
        trigger.reason,
      );
      if (scope !== this.fleetScope()) return;
      this.actionNotice.set({ tone: 'success', message: result.message });
      this.messageService.add(actionOutcomeToast('success', result.message));
    } catch (error) {
      if (scope !== this.fleetScope()) return;
      const rejection = deriveActionRejection(
        error,
        `Could not ${action.label.toLowerCase()} ${sid}.`,
      );
      this.actionNotice.set({ tone: 'danger', message: rejection.message });
      this.messageService.add(actionOutcomeToast(rejection.outcome, rejection.message, rejection.why));
      // A stale-generation refusal means the fence the operator was shown is
      // provably wrong; refresh so the next action is minted against a lane
      // they have actually seen (#2068).
      if (rejection.reasonCode === 'clerk_binding_generation_conflict') {
        void this.fleetDirectory.refresh().catch(() => {
          this.messageService.add(actionOutcomeToast('failure', LANE_FENCE_REFRESH_FAILED_MESSAGE));
        });
      }
    } finally {
      this.pendingBotIds.update((current) => {
        const next = new Set(current);
        next.delete(sid);
        return next;
      });
      if (scope === this.fleetScope()) {
        this.catalog.reload();
        // Only refresh the detail pane when the acted-on bot is still the one on
        // screen. The token is page-global, and the detail pane's journal read is
        // audit-logged: bumping it after the operator has moved on to another bot
        // would append an `EvidenceAuditEntry` asserting they read *that* bot's
        // evidence, which they did not.
        if (this.selectedSid() === sid) {
          this.detailRefreshToken.update((token) => token + 1);
        }
        this.measure('alpaca-bots-action-round-trip', startedAt);
      }
    }
  }

  private performanceNow(): number {
    return typeof performance === 'undefined' ? Date.now() : performance.now();
  }

  private mark(name: string): void {
    if (typeof performance !== 'undefined') performance.mark(name);
  }

  private measure(name: string, start: number): void {
    if (typeof performance !== 'undefined') {
      performance.measure(name, { start, end: performance.now() });
    }
  }

  private measureAfterPaint(name: string, start: number): void {
    afterNextRender(
      {
        write: () => {
          if (typeof requestAnimationFrame === 'undefined') {
            this.measure(name, start);
            return;
          }
          requestAnimationFrame(() => {
            requestAnimationFrame(() => this.measure(name, start));
          });
        },
      },
      { injector: this.injector },
    );
  }
}
