import { CurrencyPipe } from '@angular/common';
import {
  ChangeDetectionStrategy,
  Component,
  computed,
  effect,
  inject,
  input,
  resource,
  signal,
  untracked,
} from '@angular/core';
import { ButtonModule } from 'primeng/button';
import { InputText } from 'primeng/inputtext';
import { Table, TableModule } from 'primeng/table';
import { FilterService } from 'primeng/api';

import type {
  BrokerPortfolioHistory,
  PortfolioHistoryRange,
} from '../../../api/alpaca.types';
import type {
  ClerkTransactionHistoryResponse,
  ClerkTransactionOrigin,
  ClerkTransactionSummary,
} from '../../../api/clerk-transaction-history.types';
import {
  laneKey,
  type ResourceTarget,
  withAccount,
  withCommand,
} from '../../../fleet/resource-target';
import {
  fencedTarget,
  laneFenceIsEnforceable,
  LANE_FENCE_UNENFORCEABLE_MESSAGE,
  type LaneFence,
} from '../../../fleet/lane-fence';
import { BrokersService } from '../../../services/brokers.service';
import { AssetIdentityComponent } from '../../../shared/asset-identity';
import { formatReceiptLabel, ReceiptLabelPipe } from '../../../shared/pipes/receipt-label.pipe';
import { TimestampDisplayComponent } from '../../../shared/timestamp';
import { ClerkTransactionEvidenceDrawerComponent } from '../clerk-transaction-evidence-drawer/clerk-transaction-evidence-drawer.component';
import { AccountDeskTransactionHistoryStore } from './account-desk-transaction-history-store.service';

type HistoryScope = 'today' | '30d' | '60d';

interface HistoryWindow {
  readonly fromMs: number;
  readonly toMs: number;
}

interface TransactionTableRow {
  readonly transaction: ClerkTransactionSummary;
  readonly recordedAtMs: number;
  readonly symbol: string | null;
  readonly request: string;
  readonly execution: string;
  readonly status: string;
  readonly origin: ClerkTransactionOrigin | undefined;
  readonly fee: number | null;
  readonly feesReported: boolean;
  readonly evidence: string;
  readonly searchText: string;
}

interface FeedPresentation {
  readonly headline: string;
  readonly detail: string;
  readonly attention: boolean;
}

const SCOPE_OPTIONS: readonly HistoryScope[] = ['today', '30d', '60d'];

const SCOPE_CONFIG = {
  today: { label: 'Today', range: '1D' },
  '30d': { label: '30D', range: '30D' },
  '60d': { label: '60D', range: '60D' },
} as const satisfies Record<
  HistoryScope,
  { readonly label: string; readonly range: PortfolioHistoryRange }
>;

const MONEY = new Intl.NumberFormat('en-US', {
  style: 'currency',
  currency: 'USD',
  maximumFractionDigits: 4,
});

const LOCAL_DATE_MATCH_MODE = 'sameLocalDateMs';

/** Compares canonical timestamps to the local calendar day selected in the table filter. */
export function matchesLocalDateMs(value: unknown, filter: unknown): boolean {
  if (filter === null || filter === undefined) return true;
  if (typeof value !== 'number' || !Number.isFinite(value) || !(filter instanceof Date)) return false;

  const startOfDayMs = new Date(
    filter.getFullYear(),
    filter.getMonth(),
    filter.getDate(),
  ).getTime();
  const endOfDayMs = new Date(
    filter.getFullYear(),
    filter.getMonth(),
    filter.getDate() + 1,
  ).getTime();
  return value >= startOfDayMs && value < endOfDayMs;
}

/** Complete-window, client-filtered transaction table with receipt detail on demand. */
@Component({
  selector: 'app-account-desk-transaction-history',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [
    AssetIdentityComponent,
    ButtonModule,
    ClerkTransactionEvidenceDrawerComponent,
    CurrencyPipe,
    InputText,
    ReceiptLabelPipe,
    TableModule,
    TimestampDisplayComponent,
  ],
  templateUrl: './account-desk-transaction-history.component.html',
  styleUrl: './account-desk-transaction-history.component.scss',
})
export class AccountDeskTransactionHistoryComponent {
  readonly store = inject(AccountDeskTransactionHistoryStore);
  private readonly brokers = inject(BrokersService);
  private readonly filterService = inject(FilterService);

  readonly accountId = input<string | null>(null);
  /** Explicit desk target for broker-history reads. */
  readonly target = input<ResourceTarget | null>(null);
  /** The binding-generation fence frozen at desk-render time (#2106) —
   * combined with `target` only at the moment the acknowledgement command is
   * minted (`openReceipt`), never used to re-derive `target` itself. */
  readonly fence = input<LaneFence | null>(null);
  readonly refreshVersion = input(0);
  readonly fromMs = input<number | null>(null);
  readonly toMs = input<number | null>(null);
  readonly showScopeControl = input(true);
  readonly pageSize = input(8);

  private activeReceiptContext: string | null = null;
  protected readonly scope = signal<HistoryScope>('today');
  protected readonly scopeOptions = SCOPE_OPTIONS;
  protected readonly scopeConfig = SCOPE_CONFIG;
  protected readonly selectedTransaction = signal<ClerkTransactionSummary | null>(null);
  /** Captured with the receipt: acknowledgement cannot follow navigation. */
  protected readonly selectedTarget = signal<ResourceTarget | null>(null);
  protected readonly receiptOpener = signal<HTMLElement | null>(null);
  protected readonly acknowledgementOperator = signal('');
  protected readonly acknowledgingExternalOrderId = signal<string | null>(null);
  protected readonly acknowledgementError = signal<string | null>(null);
  /** Set when `openReceipt` refuses to mint a command because the lane's
   * fence had no known binding at desk-render time (#2106). Separate from
   * `acknowledgementError`, which only renders once a receipt is open. */
  protected readonly receiptBlockedMessage = signal<string | null>(null);

  protected readonly selectedBrokerHistory = resource<BrokerPortfolioHistory | null, {
    readonly enabled: boolean;
    readonly range: PortfolioHistoryRange;
    readonly target: ResourceTarget | null;
  }>({
    params: () => ({
      enabled: this.showScopeControl(),
      range: SCOPE_CONFIG[this.scope()].range,
      target: this.target(),
    }),
    loader: ({ params }) => params.enabled && params.target !== null
      ? this.brokers.getPortfolioHistory(params.target, params.range)
      : Promise.resolve(null),
  });

  protected readonly activeWindow = computed<HistoryWindow | null>(() => {
    const explicitFrom = this.fromMs();
    const explicitTo = this.toMs();
    if (explicitFrom !== null && explicitTo !== null) {
      return { fromMs: explicitFrom, toMs: explicitTo };
    }
    const timestamps = this.selectedBrokerHistory.value()?.timestamps;
    if (!timestamps?.length) return null;
    return {
      fromMs: timestamps[0],
      toMs: timestamps[timestamps.length - 1],
    };
  });

  protected readonly rangeUnavailable = computed(() =>
    this.showScopeControl()
    && !this.selectedBrokerHistory.isLoading()
    && (this.selectedBrokerHistory.error() !== undefined || this.activeWindow() === null),
  );

  protected readonly tableRows = computed<TransactionTableRow[]>(() =>
    this.store.rows().map(toTableRow),
  );

  protected readonly feedPresentation = computed<FeedPresentation | null>(() => {
    const feed = this.store.feed();
    return feed === null ? null : presentFeed(feed);
  });

  protected readonly globalFilterFields = ['searchText'];
  protected readonly pageSizeOptions = computed(() => {
    const size = this.pageSize();
    return [size, size * 2, size * 4];
  });
  protected readonly tablePassThrough = {
    table: { 'aria-label': 'Transaction history' },
  };

  constructor() {
    this.filterService.register(LOCAL_DATE_MATCH_MODE, matchesLocalDateMs);
    effect(() => {
      const accountId = this.accountId();
      const window = this.activeWindow();
      this.store.setTarget(this.target());
      this.refreshVersion();
      if (accountId !== null && window !== null) {
        untracked(() => void this.store.load(accountId, {
          fromMs: window.fromMs,
          toMs: window.toMs,
        }));
      }
    });
    effect(() => {
      const context = this.currentReceiptContext();
      if (context === this.activeReceiptContext) return;
      this.activeReceiptContext = context;
      this.selectedTransaction.set(null);
      this.selectedTarget.set(null);
      this.receiptOpener.set(null);
      this.acknowledgementOperator.set('');
      this.acknowledgementError.set(null);
      this.receiptBlockedMessage.set(null);
      this.acknowledgingExternalOrderId.set(null);
    });
  }

  protected selectScope(scope: HistoryScope): void {
    this.scope.set(scope);
  }

  protected inputValue(event: Event): string {
    return event.target instanceof HTMLInputElement ? event.target.value : '';
  }

  protected clearFilters(table: Table, input: HTMLInputElement): void {
    input.value = '';
    table.clear();
  }

  protected openReceipt(row: TransactionTableRow, event: MouseEvent): void {
    const target = this.target();
    if (target === null) return;
    // This target belongs to the receipt, not the live route. A later route
    // or directory change must not move an acknowledgement to another clerk
    // (#2106): the generation/epoch come from the fence frozen at desk-render
    // time, not from `target` itself, which stays live for reads.
    const fence = this.fence() ?? { bindingGeneration: null, routingEpoch: null };
    if (!laneFenceIsEnforceable(fence)) {
      this.receiptBlockedMessage.set(LANE_FENCE_UNENFORCEABLE_MESSAGE);
      return;
    }
    this.receiptBlockedMessage.set(null);
    const opener = event.currentTarget;
    this.receiptOpener.set(opener instanceof HTMLElement ? opener : null);
    this.selectedTarget.set(withCommand(
      withAccount(fencedTarget(target, fence), this.accountId() ?? target.accountId),
      'custody_command',
      this.newReceiptCommandId(),
    ));
    this.selectedTransaction.set(row.transaction);
  }

  protected onReceiptClosed(): void {
    this.selectedTransaction.set(null);
    this.selectedTarget.set(null);
    this.receiptOpener.set(null);
  }

  protected canAcknowledgeSelectedExternalOrder(): boolean {
    const transaction = this.selectedTransaction();
    return transaction?.transaction_origin === 'external'
      && transaction.lifecycle_state === 'review_required'
      && transaction.external_order_id !== null
      && transaction.external_order_id !== undefined;
  }

  protected async acknowledgeSelectedExternalOrder(): Promise<void> {
    const transaction = this.selectedTransaction();
    const accountId = this.store.accountId();
    const externalOrderId = transaction?.external_order_id;
    const operator = this.acknowledgementOperator().trim();
    const target = this.selectedTarget();
    if (
      !accountId
      || !externalOrderId
      || !operator
      || target === null
      || this.acknowledgingExternalOrderId() !== null
    ) return;

    this.acknowledgingExternalOrderId.set(externalOrderId);
    this.acknowledgementError.set(null);
    try {
      await this.brokers.acknowledgeExternalOrder(target, externalOrderId, operator);
      if (!this.isCurrent(target, accountId)) return;
      await this.store.load(accountId);
      if (!this.isCurrent(target, accountId)) return;
      this.acknowledgementOperator.set('');
      this.onReceiptClosed();
    } catch (error: unknown) {
      if (this.isCurrent(target, accountId)) {
        // The evidence drawer deliberately presents one safe retry message;
        // backend-authored detail remains available in the durable receipt.
        void error;
        this.acknowledgementError.set('Could not acknowledge this external order. Retry after reviewing the evidence.');
      }
    } finally {
      if (
        this.isCurrent(target, accountId)
        && this.acknowledgingExternalOrderId() === externalOrderId
      ) {
        this.acknowledgingExternalOrderId.set(null);
      }
    }
  }

  /** Keyed off the frozen fence, not `target()`'s live generation (#2106): a
   * directory rebind alone must not read as "the desk changed" and reset an
   * open receipt or discard an in-flight acknowledgement's result — only an
   * actual desk change (clerk/account) or a fence recompute (route change)
   * does. */
  private currentReceiptContext(): string | null {
    const target = this.target();
    if (target === null) return null;
    const fenced = fencedTarget(target, this.fence() ?? { bindingGeneration: null, routingEpoch: null });
    const accountId = this.accountId() ?? fenced.accountId;
    return laneKey(
      fenced.broker,
      fenced.clerkId,
      fenced.routingEpoch,
      fenced.bindingGeneration,
      accountId,
    );
  }

  private isCurrent(target: ResourceTarget, accountId: string): boolean {
    return this.currentReceiptContext() === laneKey(
      target.broker,
      target.clerkId,
      target.routingEpoch,
      target.bindingGeneration,
      accountId,
    );
  }

  private newReceiptCommandId(): string {
    if (typeof globalThis.crypto?.randomUUID !== 'function') {
      throw new Error('This browser cannot create a durable request identity.');
    }
    return globalThis.crypto.randomUUID();
  }
}

function toTableRow(transaction: ClerkTransactionSummary): TransactionTableRow {
  const symbol = transaction.order_instruction?.symbol ?? null;
  const request = requestLabel(transaction);
  const execution = executionLabel(transaction);
  const status = formatReceiptLabel(transaction.lifecycle_state);
  const feesReported = transaction.fee_fidelity === 'reported'
    || transaction.commission_status === 'reported';
  const evidence = `${transaction.event_count} event${transaction.event_count === 1 ? '' : 's'}`;
  const searchableValues = [
    symbol,
    request,
    execution,
    status,
    formatReceiptLabel(transaction.transaction_origin),
    transaction.transaction_kind,
    transaction.transaction_origin,
    transaction.transaction_id,
    transaction.subject_id,
    transaction.strategy_instance_id,
    transaction.run_id,
    transaction.intent_id,
    transaction.order_ref,
    transaction.order_id,
    transaction.perm_id,
    transaction.exec_id,
    transaction.native_order_id,
    transaction.native_execution_id,
    transaction.external_order_id,
    transaction.fee,
    transaction.event_count,
  ];

  return {
    transaction,
    recordedAtMs: transaction.recorded_at_ms,
    symbol,
    request,
    execution,
    status,
    origin: transaction.transaction_origin,
    fee: transaction.fee,
    feesReported,
    evidence,
    searchText: searchableValues
      .filter((value) => value !== null && value !== undefined)
      .join(' '),
  };
}

function requestLabel(transaction: ClerkTransactionSummary): string {
  const instruction = transaction.order_instruction;
  if (instruction === undefined) return 'No request details recorded';
  const action = instruction.action ? formatReceiptLabel(instruction.action) : 'Order';
  const quantity = instruction.quantity === null ? '' : ` ${instruction.quantity}`;
  const orderType = instruction.order_type ? ` · ${formatReceiptLabel(instruction.order_type)}` : '';
  const limit = instruction.limit_price === null ? '' : ` at ${MONEY.format(instruction.limit_price)}`;
  const stop = instruction.stop_price === null ? '' : ` · Stop ${MONEY.format(instruction.stop_price)}`;
  return `${action}${quantity}${orderType}${limit}${stop}`;
}

function executionLabel(transaction: ClerkTransactionSummary): string {
  if (transaction.execution_quantity === null || transaction.execution_quantity === undefined) {
    return 'No execution recorded';
  }
  if (transaction.execution_price === null || transaction.execution_price === undefined) {
    return `${transaction.execution_quantity} filled`;
  }
  return `${transaction.execution_quantity} filled at ${MONEY.format(transaction.execution_price)}`;
}

function presentFeed(feed: ClerkTransactionHistoryResponse): FeedPresentation {
  switch (feed.feed_state) {
    case 'live':
      return {
        headline: 'History is current',
        detail: 'Broker activity in this period is ready to search and review.',
        attention: false,
      };
    case 'reconnecting':
      return {
        headline: 'History is reconnecting',
        detail: 'Saved records remain available while new broker updates reconnect.',
        attention: true,
      };
    case 'rebuilding':
      return {
        headline: 'History is being rebuilt',
        detail: 'The table may change as saved broker records are restored.',
        attention: true,
      };
    case 'stale':
      return {
        headline: 'History may be delayed',
        detail: 'Review the technical feed details before relying on the newest row.',
        attention: true,
      };
    case 'offline_but_saved':
      return {
        headline: 'Showing saved history',
        detail: 'New broker activity will appear after the connection returns.',
        attention: true,
      };
    case 'corrupt':
      return {
        headline: 'History needs attention',
        detail: 'The saved transaction view cannot currently be relied on.',
        attention: true,
      };
    case 'projection_unavailable':
      return {
        headline: 'History is unavailable',
        detail: 'The account transaction view could not be prepared.',
        attention: true,
      };
  }
}
