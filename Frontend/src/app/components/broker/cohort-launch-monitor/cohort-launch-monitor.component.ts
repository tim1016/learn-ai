import { DecimalPipe } from '@angular/common';
import { ChangeDetectionStrategy, Component, computed, effect, inject, input, signal } from '@angular/core';

import type { CohortBatchLaunchMemberOutcome, CohortBatchLaunchStatus } from '../../../api/cohort-batch-launch.types';
import type { LiveInstanceStatus } from '../../../api/live-instances.types';
import type { LiveRunStatus } from '../../../api/live-runs.types';
import { SectionErrorComponent } from '../../../shared/errors/section-error.component';
import { ReceiptLabelPipe } from '../../../shared/pipes/receipt-label.pipe';
import { TimestampDisplayComponent } from '../../../shared/timestamp';
import { LiveRunsService } from '../../../services/live-runs.service';

interface CohortMemberTelemetry {
  strategyInstanceId: string;
  instance: LiveInstanceStatus | null;
  run: LiveRunStatus | null;
  error: string | null;
}

interface LastFillView {
  label: string;
  atMs: number | null;
}

interface CohortMonitorRow {
  strategyInstanceId: string;
  outcome: CohortBatchLaunchMemberOutcome | null;
  receiptState: string;
  runId: string | null;
  clientId: number | null;
  effectiveCap: number | null;
  ordersUsed: number | null;
  roundTrips: number | null;
  namespaceExposure: string | null;
  lastFill: LastFillView;
  uptimeMs: number | null;
  flatAfterExit: boolean | null;
  incident: boolean;
  statusError: string | null;
}

interface CohortSuccessMeter {
  targetCount: number;
  concurrentUptimeMs: number;
  totalOrders: number;
  roundTrips: number;
  flatAfterExitCount: number;
  incidentCount: number;
}

@Component({
  selector: 'app-cohort-launch-monitor',
  imports: [DecimalPipe, ReceiptLabelPipe, SectionErrorComponent, TimestampDisplayComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './cohort-launch-monitor.component.html',
  styleUrl: './cohort-launch-monitor.component.scss',
})
export class CohortLaunchMonitorComponent {
  private readonly liveRuns = inject(LiveRunsService);
  private loadEpoch = 0;

  readonly accountId = input.required<string | null>();
  readonly reloadVersion = input<number>(0);
  readonly cohort = signal<CohortBatchLaunchStatus | null>(null);
  readonly telemetry = signal<readonly CohortMemberTelemetry[]>([]);
  readonly loading = signal(false);
  readonly error = signal<string | null>(null);
  readonly refreshedAtMs = signal<number | null>(null);

  readonly rows = computed<CohortMonitorRow[]>(() => {
    const cohort = this.cohort();
    if (cohort === null) return [];
    const outcomes = new Map(cohort.outcomes.map((outcome) => [outcome.strategy_instance_id, outcome]));
    const telemetry = new Map(this.telemetry().map((row) => [row.strategyInstanceId, row]));
    const nowMs = this.refreshedAtMs();
    return cohort.member_strategy_instance_ids.map((strategyInstanceId) =>
      toCohortMonitorRow(
        strategyInstanceId,
        outcomes.get(strategyInstanceId) ?? null,
        cohort.outcomes_state,
        telemetry.get(strategyInstanceId) ?? null,
        nowMs,
      ),
    );
  });

  readonly successMeter = computed<CohortSuccessMeter | null>(() => {
    const cohort = this.cohort();
    if (cohort === null) return null;
    const rows = this.rows();
    return {
      targetCount: cohort.member_strategy_instance_ids.length,
      concurrentUptimeMs: rows.reduce((total, row) => total + (row.uptimeMs ?? 0), 0),
      totalOrders: rows.reduce((total, row) => total + (row.ordersUsed ?? 0), 0),
      roundTrips: rows.reduce((total, row) => total + (row.roundTrips ?? 0), 0),
      flatAfterExitCount: rows.filter((row) => row.flatAfterExit === true).length,
      incidentCount: rows.filter((row) => row.incident).length,
    };
  });

  constructor() {
    effect(() => {
      const accountId = this.accountId();
      const reloadVersion = this.reloadVersion();
      void reloadVersion;
      void this.load(accountId);
    });
  }

  async refresh(): Promise<void> {
    await this.load(this.accountId());
  }

  private async load(accountId: string | null): Promise<void> {
    const epoch = ++this.loadEpoch;
    if (accountId === null) {
      this.cohort.set(null);
      this.telemetry.set([]);
      this.error.set(null);
      this.refreshedAtMs.set(null);
      return;
    }
    this.loading.set(true);
    this.error.set(null);
    try {
      const cohort = await this.liveRuns.getLatestCohortBatchLaunch(accountId);
      if (epoch !== this.loadEpoch) return;
      this.cohort.set(cohort);
      if (cohort === null) {
        this.telemetry.set([]);
        this.refreshedAtMs.set(Date.now());
        return;
      }
      const telemetry = await Promise.all(
        cohort.member_strategy_instance_ids.map((strategyInstanceId) =>
          this.loadMemberTelemetry(strategyInstanceId),
        ),
      );
      if (epoch !== this.loadEpoch) return;
      this.telemetry.set(telemetry);
      this.refreshedAtMs.set(Date.now());
    } catch (error) {
      if (epoch === this.loadEpoch) {
        this.error.set(humanError(error));
        this.cohort.set(null);
        this.telemetry.set([]);
      }
    } finally {
      if (epoch === this.loadEpoch) this.loading.set(false);
    }
  }

  private async loadMemberTelemetry(strategyInstanceId: string): Promise<CohortMemberTelemetry> {
    try {
      const instance = await this.liveRuns.getInstanceStatus(strategyInstanceId);
      const runId = instance.live_binding?.run_id ?? instance.evidence_binding?.run_id ?? null;
      if (runId === null) return { strategyInstanceId, instance, run: null, error: null };
      try {
        const run = await this.liveRuns.getStatus(runId);
        return { strategyInstanceId, instance, run, error: null };
      } catch (error) {
        return { strategyInstanceId, instance, run: null, error: humanError(error) };
      }
    } catch (error) {
      return {
        strategyInstanceId,
        instance: null,
        run: null,
        error: humanError(error),
      };
    }
  }
}

function toCohortMonitorRow(
  strategyInstanceId: string,
  outcome: CohortBatchLaunchMemberOutcome | null,
  outcomesState: CohortBatchLaunchStatus['outcomes_state'],
  telemetry: CohortMemberTelemetry | null,
  nowMs: number | null,
): CohortMonitorRow {
  const instance = telemetry?.instance ?? null;
  const run = telemetry?.run ?? null;
  const isRunning = instance?.process.state === 'running';
  const startedAtMs = instance?.process.started_at_ms ?? null;
  const ownedPositions = instance?.broker?.owned_positions ?? null;
  const flatAfterExit = isRunning || ownedPositions === null
    ? null
    : Object.values(ownedPositions).every((quantity) => quantity === 0);
  return {
    strategyInstanceId,
    outcome,
    receiptState: outcome?.state ?? outcomesState,
    runId: instance?.live_binding?.run_id ?? instance?.evidence_binding?.run_id ?? null,
    clientId: instance?.process.ibkr_client_id ?? null,
    effectiveCap: instance?.readiness?.orders_cap ?? instance?.start_defaults?.max_orders_per_day ?? null,
    ordersUsed: instance?.readiness?.orders_used ?? run?.executions.row_count ?? null,
    roundTrips: run?.trades.row_count ?? null,
    namespaceExposure: namespaceExposure(instance),
    lastFill: lastFill(run),
    uptimeMs: isRunning && startedAtMs !== null && nowMs !== null
      ? Math.max(0, nowMs - startedAtMs)
      : null,
    flatAfterExit,
    incident: outcome?.state === 'blocked' || outcome?.state === 'skipped' || instance?.process.state === 'unreachable',
    statusError: telemetry?.error ?? null,
  };
}

function namespaceExposure(instance: LiveInstanceStatus | null): string | null {
  const broker = instance?.broker;
  if (broker === null || broker === undefined) return null;
  const positions = Object.entries(broker.owned_positions)
    .map(([symbol, quantity]) => `${symbol} ${quantity}`)
    .join(', ');
  return positions ? `${broker.bot_order_namespace}: ${positions}` : `${broker.bot_order_namespace}: flat`;
}

function lastFill(run: LiveRunStatus | null): LastFillView {
  const fills = run?.executions.last_fills ?? [];
  const fill = fills.length > 0 ? fills[fills.length - 1] : null;
  if (fill === null) return { label: 'No broker-confirmed fill', atMs: null };
  const symbol = recordString(fill, 'symbol') ?? 'Unknown symbol';
  const quantity = recordNumber(fill, 'fill_quantity');
  const price = recordNumber(fill, 'fill_price');
  const quantityLabel = quantity === null ? 'quantity unavailable' : String(quantity);
  const priceLabel = price === null ? 'price unavailable' : String(price);
  return {
    label: `${symbol} ${quantityLabel} @ ${priceLabel}`,
    atMs: recordNumber(fill, 'ts_ms'),
  };
}

function recordString(record: Record<string, unknown>, key: string): string | null {
  const value = record[key];
  return typeof value === 'string' && value.length > 0 ? value : null;
}

function recordNumber(record: Record<string, unknown>, key: string): number | null {
  const value = record[key];
  return typeof value === 'number' && Number.isFinite(value) ? value : null;
}

function humanError(error: unknown): string {
  return error instanceof Error ? error.message : 'The live cohort status is unavailable.';
}
