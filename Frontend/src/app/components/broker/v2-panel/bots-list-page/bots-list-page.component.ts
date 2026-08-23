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
import { AlpacaDeployDrawerComponent } from '../../broker-deploy-page/alpaca-deploy-drawer.component';
import { AccountStripComponent } from '../account-strip/account-strip.component';
import { BotTriageDetailComponent } from '../bot-triage-detail/bot-triage-detail.component';
import { BotsRosterComponent } from '../bots-roster/bots-roster.component';
import { BrokerV2PanelService } from '../lib/broker-v2-panel.service';
import type { BotCatalogView, PanelActionTrigger } from '../lib/broker-v2-panel.types';
import { actionOutcomeToast, deriveActionRejection } from '../lib/panel-action-outcome';

const CATALOG_POLL_MS = 5_000;
const ACCOUNT_POLL_MS = 15_000;

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
    BotTriageDetailComponent,
    BotsRosterComponent,
    RouterLink,
  ],
  templateUrl: './bots-list-page.component.html',
  styleUrl: './bots-list-page.component.scss',
  host: { class: 'block h-full' },
})
export class BotsListPageComponent {
  readonly broker = input('alpaca');
  readonly accountId = input.required<string>();

  private readonly brokersService = inject(BrokersService);
  private readonly panelService = inject(BrokerV2PanelService);
  private readonly destroyRef = inject(DestroyRef);
  private readonly injector = inject(Injector);
  private readonly document = inject(DOCUMENT);
  private readonly messageService = inject(MessageService);
  private readonly fleetScope = computed(() => `${this.broker()}:${this.accountId()}`);
  private readonly catalogSnapshot = signal<ScopedSnapshot<BotCatalogView[]> | null>(null);
  private readonly accountSnapshot = signal<ScopedSnapshot<BrokerAccountSnapshot> | null>(null);
  private readonly clerkSnapshot = signal<ScopedSnapshot<ClerkStatus> | null>(null);

  protected readonly actionNotice = signal<{ tone: 'success' | 'danger'; message: string } | null>(
    null,
  );
  protected readonly pendingBotIds = signal<ReadonlySet<string>>(new Set());
  protected readonly deployOpen = signal(false);
  private readonly requestedSid = signal<string | null>(null);
  /** Bumped after an action lands so the detail pane refetches its panel. */
  protected readonly detailRefreshToken = signal(0);

  protected readonly catalog = resource({
    params: () => ({ broker: this.broker(), accountId: this.accountId() }),
    loader: async ({ params }) => {
      const startedAt = this.performanceNow();
      const bots = await this.panelService.getCatalog(params.broker, params.accountId);
      const scope = `${params.broker}:${params.accountId}`;
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
    params: () => ({ broker: this.broker(), accountId: this.accountId() }),
    loader: async ({ params }) => {
      const snapshot = await this.brokersService.getAccount(params.broker);
      if (snapshot.account_id !== params.accountId) {
        throw new Error(
          `Alpaca confirmed account ${snapshot.account_id}, not routed account ${params.accountId}.`,
        );
      }
      this.accountSnapshot.set({
        scope: `${params.broker}:${params.accountId}`,
        updatedAtMs: snapshot.observed_at_ms,
        value: snapshot,
      });
      return snapshot;
    },
  });

  protected readonly clerkStatus = resource({
    params: () => ({ broker: this.broker(), accountId: this.accountId() }),
    loader: async ({ params }) => {
      const snapshot = await this.brokersService.getClerkStatus(params.broker);
      if (snapshot.account_id !== params.accountId) {
        throw new Error(
          `The Clerk is observing account ${snapshot.account_id}, not routed account ${params.accountId}.`,
        );
      }
      this.clerkSnapshot.set({
        scope: `${params.broker}:${params.accountId}`,
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
  protected readonly stale = computed(
    () => Boolean(this.catalog.error()) && this.bots().length > 0,
  );
  constructor() {
    afterNextRender(() => this.mark('alpaca-bots-route-shell'));

    const catalogTimer = setInterval(() => {
      if (this.document.visibilityState === 'visible' && !this.catalog.isLoading()) {
        this.catalog.reload();
      }
    }, CATALOG_POLL_MS);
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
    if (sid === null || this.pendingBotIds().has(sid)) return;

    const action = trigger.action;
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
        this.broker(),
        this.accountId(),
        sid,
        action,
        trigger.reason,
      );
      this.actionNotice.set({ tone: 'success', message: result.message });
      this.messageService.add(actionOutcomeToast('success', result.message));
    } catch (error) {
      const rejection = deriveActionRejection(
        error,
        `Could not ${action.label.toLowerCase()} ${sid}.`,
      );
      this.actionNotice.set({ tone: 'danger', message: rejection.message });
      this.messageService.add(actionOutcomeToast(rejection.outcome, rejection.message, rejection.why));
    } finally {
      this.pendingBotIds.update((current) => {
        const next = new Set(current);
        next.delete(sid);
        return next;
      });
      this.catalog.reload();
      this.detailRefreshToken.update((token) => token + 1);
      this.measure('alpaca-bots-action-round-trip', startedAt);
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
