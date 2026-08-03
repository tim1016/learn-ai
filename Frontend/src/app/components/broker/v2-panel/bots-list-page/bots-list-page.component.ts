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

import type { BrokerAccountSnapshot, ClerkStatus } from '../../../../api/alpaca.types';
import { BrokersService } from '../../../../services/brokers.service';
import { ReceiptLabelPipe } from '../../../../shared/pipes/receipt-label.pipe';
import { TimestampDisplayComponent } from '../../../../shared/timestamp/timestamp-display.component';
import { AccountStripComponent } from '../account-strip/account-strip.component';
import { BotsRosterComponent, type RowActionEvent } from '../bots-roster/bots-roster.component';
import { BrokerV2PanelService } from '../lib/broker-v2-panel.service';
import type { BotCatalogView } from '../lib/broker-v2-panel.types';

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
    BotsRosterComponent,
    RouterLink,
    ReceiptLabelPipe,
    TimestampDisplayComponent,
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
  private readonly fleetScope = computed(() => `${this.broker()}:${this.accountId()}`);
  private readonly catalogSnapshot = signal<ScopedSnapshot<BotCatalogView[]> | null>(null);
  private readonly accountSnapshot = signal<ScopedSnapshot<BrokerAccountSnapshot> | null>(null);
  private readonly clerkSnapshot = signal<ScopedSnapshot<ClerkStatus> | null>(null);

  protected readonly actionNotice = signal<{ tone: 'success' | 'danger'; message: string } | null>(
    null,
  );
  protected readonly pendingBotIds = signal<ReadonlySet<string>>(new Set());

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

  protected async onRowAction(event: RowActionEvent): Promise<void> {
    const sid = event.bot.strategy_instance_id;
    if (this.pendingBotIds().has(sid)) return;
    const startedAt = this.performanceNow();
    this.actionNotice.set(null);
    this.pendingBotIds.update((current) => new Set([...current, sid]));

    try {
      const action = event.bot.row_action;
      if (action?.action_id !== event.action || !action.enabled) {
        this.actionNotice.set({
          tone: 'danger',
          message: `${event.action === 'resume' ? 'Resume' : 'Stop'} is no longer available for ${sid}. Refreshing its current state.`,
        });
        this.catalog.reload();
        return;
      }

      const result = await this.panelService.runBotAction(
        this.broker(),
        this.accountId(),
        sid,
        action,
      );
      this.actionNotice.set({ tone: 'success', message: result.message });
      this.catalog.reload();
    } catch (error) {
      this.actionNotice.set({
        tone: 'danger',
        message: error instanceof Error ? error.message : `Could not ${event.action} ${sid}.`,
      });
    } finally {
      this.pendingBotIds.update((current) => {
        const next = new Set(current);
        next.delete(sid);
        return next;
      });
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
