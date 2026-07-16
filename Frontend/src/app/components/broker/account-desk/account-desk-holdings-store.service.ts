import {
  DestroyRef,
  Injectable,
  Injector,
  computed,
  effect,
  inject,
  runInInjectionContext,
  signal,
} from '@angular/core';

import type {
  AccountTruthFactOwner,
  AccountTruthPositionRow,
  AccountTruthResponse,
  IbkrAccountSummary,
  IbkrPnLTick,
  IbkrPosition,
  IbkrPositionsSnapshot,
} from '../../../api/broker-models';
import {
  operatorBlockersForAccountDeskLens,
  type OperatorBlocker,
} from '../../../api/operator-blocker.types';
import { BrokerService } from '../../../services/broker.service';
import { brokerSse, type SseStream } from '../../../services/broker-sse';

interface AttestedHoldings {
  readonly account: IbkrAccountSummary;
  readonly positions: IbkrPositionsSnapshot;
  readonly truthByConId: ReadonlyMap<number, AccountTruthPositionRow>;
  readonly blockers: readonly OperatorBlocker[];
}

export interface AccountDeskHoldingRow {
  readonly position: IbkrPosition;
  readonly owner: AccountTruthFactOwner;
  readonly pnl: IbkrPnLTick | null;
  readonly blockers: readonly OperatorBlocker[];
}

export interface AccountDeskHeadlineMetrics {
  readonly equity: number | null;
  readonly cash: number | null;
  readonly buyingPower: number | null;
  readonly dayPnl: number | null;
  readonly openPositions: number;
}

/**
 * Route-scoped broker holdings projection. It admits broker data only after
 * every snapshot has attested the route account ID; the P&L EventSources are
 * opened only after that boundary succeeds.
 */
@Injectable()
export class AccountDeskHoldingsStore {
  private readonly broker = inject(BrokerService);
  private readonly destroyRef = inject(DestroyRef);
  private readonly injector = inject(Injector);
  private requestGeneration = 0;

  private readonly accountKey = signal<string | null>(null);
  private readonly holdingsState = signal<AttestedHoldings | null>(null);
  private readonly loadingState = signal(false);
  private readonly errorState = signal<unknown>(null);
  private readonly unavailableMessageState = signal<string | null>(null);
  private readonly accountStream = signal<SseStream<IbkrPnLTick> | null>(null);
  private readonly positionStream = signal<SseStream<IbkrPnLTick> | null>(null);
  private readonly accountTickState = signal<IbkrPnLTick | null>(null);
  private readonly positionTicks = signal<ReadonlyMap<number, IbkrPnLTick>>(new Map());

  readonly accountId = this.accountKey.asReadonly();
  readonly loading = this.loadingState.asReadonly();
  readonly error = this.errorState.asReadonly();
  readonly unavailableMessage = this.unavailableMessageState.asReadonly();
  readonly hasLastGood = computed(() => this.holdingsState() !== null);
  readonly showingStaleLastGood = computed(() =>
    this.holdingsState() !== null &&
    (this.errorState() !== null || this.unavailableMessageState() !== null),
  );
  readonly account = computed(() => this.holdingsState()?.account ?? null);
  /** Shared, backend-authored guidance projections for the current holdings evidence. */
  readonly operatorBlockers = computed(() => this.holdingsState()?.blockers ?? []);
  readonly headlineMetrics = computed<AccountDeskHeadlineMetrics | null>(() => {
    const holdings = this.holdingsState();
    if (holdings === null) return null;
    return {
      equity: holdings.account.net_liquidation ?? null,
      cash: holdings.account.cash_balance ?? null,
      buyingPower: holdings.account.buying_power ?? null,
      dayPnl: this.accountTickState()?.daily_pnl ?? holdings.account.day_pnl ?? null,
      openPositions: holdings.positions.positions.length,
    };
  });
  readonly rows = computed<readonly AccountDeskHoldingRow[]>(() => {
    const holdings = this.holdingsState();
    if (holdings === null) return [];
    const ticks = this.positionTicks();
    return holdings.positions.positions.map((position) => {
      const truth = holdings.truthByConId.get(position.con_id);
      if (truth === undefined) {
        throw new Error('Attested holdings must include Account Truth ownership for every position.');
      }
      return {
        position,
        owner: truth.owner,
        pnl: ticks.get(position.con_id) ?? null,
        blockers: operatorBlockersForAccountDeskLens(
          holdings.blockers.filter(
            (blocker) =>
              blocker.anchor.kind === 'holdings_row' &&
              blocker.anchor.subject_key === String(position.con_id),
          ),
          'trader',
        ),
      };
    });
  });

  constructor() {
    this.destroyRef.onDestroy(() => {
      this.requestGeneration += 1;
      this.closeStreams();
    });

    effect(() => {
      const stream = this.accountStream();
      if (stream === null) return;
      this.consumeAccountStream(stream);
    });
    effect(() => {
      const stream = this.positionStream();
      if (stream === null) return;
      this.consumePositionStream(stream);
    });
  }

  async load(accountId: string): Promise<void> {
    if (this.accountKey() !== accountId) {
      this.requestGeneration += 1;
      this.accountKey.set(accountId);
      this.holdingsState.set(null);
      this.errorState.set(null);
      this.unavailableMessageState.set(null);
      this.closeStreams();
    }
    const generation = ++this.requestGeneration;
    this.loadingState.set(true);
    this.errorState.set(null);
    this.unavailableMessageState.set(null);
    try {
      const account = await this.broker.account();
      if (generation !== this.requestGeneration) return;
      if (account.account_id !== accountId) {
        this.clearForIdentityFailure(
          'The connected broker session is attached to a different account. Live holdings are unavailable.',
        );
        return;
      }

      const [positions, truth] = await Promise.all([this.broker.positions(), this.broker.accountTruth()]);
      if (generation !== this.requestGeneration) return;
      const truthByConId = attestedTruthPositions(accountId, positions, truth);
      if (positions.account_id !== accountId || truthByConId === null) {
        this.clearForIdentityFailure(
          'Broker account evidence did not attest this route. Live holdings are unavailable.',
        );
        return;
      }

      this.holdingsState.set({
        account,
        positions,
        truthByConId,
        blockers: truth.operator_blockers.filter((blocker) => blocker.host === 'account_desk'),
      });
      this.openStreams(positions.positions);
    } catch (error) {
      if (generation !== this.requestGeneration) return;
      this.errorState.set(error);
      this.closeStreams();
    } finally {
      if (generation === this.requestGeneration) this.loadingState.set(false);
    }
  }

  retry(): void {
    const accountId = this.accountKey();
    if (accountId) void this.load(accountId);
  }

  private consumeAccountStream(stream: SseStream<IbkrPnLTick>): void {
    if (stream.status() === 'error' || stream.status() === 'closed') {
      this.stopStreaming('The account P&L stream is disconnected. Showing the last attested holdings.');
      return;
    }
    if (stream.lastError() !== null) {
      this.clearForIdentityFailure('The account P&L stream returned malformed data. Live holdings are unavailable.');
      return;
    }
    const tick = stream.latest();
    if (tick === null) return;
    const accountId = this.accountKey();
    if (!accountId || !isPnlTickForAccount(tick, accountId, null)) {
      this.clearForIdentityFailure('The account P&L stream could not attest this route. Live holdings are unavailable.');
      return;
    }
    this.accountTickState.set(tick);
  }

  private consumePositionStream(stream: SseStream<IbkrPnLTick>): void {
    if (stream.status() === 'error' || stream.status() === 'closed') {
      this.stopStreaming('The position P&L stream is disconnected. Showing the last attested holdings.');
      return;
    }
    if (stream.lastError() !== null) {
      this.clearForIdentityFailure('The position P&L stream returned malformed data. Live holdings are unavailable.');
      return;
    }
    const accountId = this.accountKey();
    const holdings = this.holdingsState();
    if (!accountId || holdings === null) return;
    const expectedConIds = new Set(holdings.positions.positions.map((position) => position.con_id));
    const next = new Map<number, IbkrPnLTick>();
    for (const tick of stream.data()) {
      if (!isPnlTickForAccount(tick, accountId, expectedConIds)) {
        this.clearForIdentityFailure('The position P&L stream could not attest this route. Live holdings are unavailable.');
        return;
      }
      if (tick.con_id !== null) next.set(tick.con_id, tick);
    }
    this.positionTicks.set(next);
  }

  private openStreams(positions: readonly IbkrPosition[]): void {
    this.closeStreams();
    this.accountTickState.set(null);
    this.positionTicks.set(new Map());
    this.accountStream.set(
      runInInjectionContext(this.injector, () =>
        brokerSse<IbkrPnLTick>('/api/broker/pnl/stream?debounce_ms=1000', 'pnl', { maxBuffer: 1 }),
      ),
    );
    if (positions.length === 0) return;

    const conIds = [...new Set(positions.map((position) => position.con_id))];
    const query = conIds.map((conId) => `con_ids=${conId}`).join('&');
    this.positionStream.set(
      runInInjectionContext(this.injector, () =>
        brokerSse<IbkrPnLTick>(
          `/api/broker/pnl/positions/stream?${query}&debounce_ms=1000`,
          'pnl',
          { maxBuffer: conIds.length * 60 },
        ),
      ),
    );
  }

  private stopStreaming(message: string): void {
    this.unavailableMessageState.set(message);
    this.closeStreams();
  }

  private clearForIdentityFailure(message: string): void {
    this.holdingsState.set(null);
    this.errorState.set(null);
    this.unavailableMessageState.set(message);
    this.closeStreams();
  }

  private closeStreams(): void {
    this.accountStream()?.close();
    this.positionStream()?.close();
    this.accountStream.set(null);
    this.positionStream.set(null);
    this.accountTickState.set(null);
    this.positionTicks.set(new Map());
  }
}

function attestedTruthPositions(
  accountId: string,
  positions: IbkrPositionsSnapshot,
  truth: AccountTruthResponse,
): ReadonlyMap<number, AccountTruthPositionRow> | null {
  if (truth.account_id !== accountId) return null;
  const truthByConId = new Map(truth.positions.map((position) => [position.con_id, position]));
  for (const position of positions.positions) {
    const truthPosition = truthByConId.get(position.con_id);
    if (
      truthPosition === undefined ||
      truthPosition.account_id !== accountId ||
      truthPosition.quantity !== position.quantity
    ) {
      return null;
    }
  }
  return truthByConId;
}

function isPnlTickForAccount(
  value: unknown,
  accountId: string,
  expectedConIds: ReadonlySet<number> | null,
): value is IbkrPnLTick {
  if (!isRecord(value) || value['account_id'] !== accountId || !isNullableFiniteNumber(value['con_id'])) {
    return false;
  }
  if (typeof value['ts_ms'] !== 'number' || !Number.isSafeInteger(value['ts_ms'])) return false;
  for (const field of ['daily_pnl', 'unrealized_pnl', 'realized_pnl', 'market_value', 'position']) {
    if (!isNullableFiniteNumber(value[field])) return false;
  }
  const conId = value['con_id'];
  return expectedConIds === null ? conId === null : conId !== null && expectedConIds.has(conId);
}

function isNullableFiniteNumber(value: unknown): value is number | null {
  return value === null || (typeof value === 'number' && Number.isFinite(value));
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}
