import { HttpErrorResponse } from '@angular/common/http';
import {
  ChangeDetectionStrategy,
  Component,
  computed,
  effect,
  inject,
  input,
  output,
  resource,
  signal,
  untracked,
} from '@angular/core';
import { RouterLink } from '@angular/router';

import { ReceiptLabelPipe } from '../../../shared/pipes/receipt-label.pipe';
import { TimestampDisplayComponent } from '../../../shared/timestamp';
import {
  BrokersService,
  type SqliteTimelineQuery,
} from '../../../services/brokers.service';
import { laneKey, type ResourceTarget, withAccount, withCommand } from '../../../fleet/resource-target';
import type {
  SqliteRecoveryAction,
  SqliteSafeFlattenPlan,
  SqliteTimelineEntry,
} from '../../../api/alpaca.types';
import { SafeFlattenPlanComponent } from '../../broker/shared/safe-flatten-plan/safe-flatten-plan.component';
import { TypedHaltConfirmComponent } from '../../broker/shared/typed-halt-confirm/typed-halt-confirm.component';
import {
  type ActionReceiptView,
  PanelActionReceiptComponent,
} from '../../broker/v2-panel/panel-shell/panel-action-receipt.component';

interface ActionProblem {
  readonly reason: string | null;
  readonly message: string;
  readonly remediation: string | null;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null;
}

function actionProblem(error: unknown, fallback: string): ActionProblem {
  if (!(error instanceof HttpErrorResponse)) {
    return { reason: null, message: fallback, remediation: null };
  }
  const detail = error.error?.detail;
  if (!isRecord(detail)) {
    return {
      reason: null,
      message:
        error.status === 409
          ? 'Clerk evidence changed. Review the refreshed action before trying again.'
          : fallback,
      remediation: null,
    };
  }
  const capability = detail['capability'];
  const capabilityNextStep =
    isRecord(capability) && typeof capability['next_step'] === 'string'
      ? capability['next_step']
      : null;
  return {
    reason: typeof detail['reason'] === 'string' ? detail['reason'] : null,
    message: typeof detail['message'] === 'string' ? detail['message'] : fallback,
    remediation:
      typeof detail['remediation'] === 'string'
        ? detail['remediation']
        : typeof detail['next_step'] === 'string'
          ? detail['next_step']
          : capabilityNextStep,
  };
}

/** Existing Alpaca Desk adapter for the boot-selected SQLite Clerk authority. */
@Component({
  selector: 'app-alpaca-sqlite-custody',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [
    PanelActionReceiptComponent,
    ReceiptLabelPipe,
    RouterLink,
    SafeFlattenPlanComponent,
    TimestampDisplayComponent,
    TypedHaltConfirmComponent,
  ],
  templateUrl: './alpaca-sqlite-custody.component.html',
  styleUrl: './alpaca-sqlite-custody.component.scss',
})
export class AlpacaSqliteCustodyComponent {
  readonly accountId = input.required<string>();
  readonly target = input.required<ResourceTarget>();
  protected readonly operatorLensQuery = { lens: 'operator' } as const;
  readonly projectionRefreshVersion = input(0);
  readonly timelineQuery = input<SqliteTimelineQuery | null>(null);
  readonly projectionInvalidated = output();
  private readonly brokers = inject(BrokersService);
  private requireClerkId(): string { return this.target().clerkId; }

  protected readonly projection = resource({
    params: () => ({
      target: this.target(),
      accountId: this.accountId(),
      refreshVersion: this.projectionRefreshVersion(),
    }),
    loader: ({ params }) => this.brokers.getSqliteClerkProjection(params.target.clerkId, params.accountId),
  });
  protected readonly timeline = signal<readonly SqliteTimelineEntry[]>([]);
  protected readonly timelineFilters = signal<SqliteTimelineQuery>({});
  private readonly timelineAppliedFilters = signal<SqliteTimelineQuery>({});
  protected readonly timelineOpen = signal(false);
  /** A loading indicator belongs to the lane that started its request. */
  private readonly timelineLoadingProvenance = signal<string | null>(null);
  private readonly timelineLoadingMoreProvenance = signal<string | null>(null);
  protected readonly timelineLoading = computed(
    () => this.timelineLoadingProvenance() === this.currentProvenance(),
  );
  protected readonly timelineLoadingMore = computed(
    () => this.timelineLoadingMoreProvenance() === this.currentProvenance(),
  );
  private readonly queuedTimelineQuery = signal<SqliteTimelineQuery | null>(null);
  protected readonly timelineNextCursor = signal<string | null>(null);
  protected readonly timelineTotalEntries = signal(0);
  protected readonly busyActionId = signal<string | null>(null);
  protected readonly actionNotice = signal<string | null>(null);
  protected readonly actionProblem = signal<ActionProblem | null>(null);
  protected readonly confirmationAction = signal<SqliteRecoveryAction | null>(null);
  /** Captured when the confirmation opens, never re-read at submit time. */
  private readonly confirmationTarget = signal<ResourceTarget | null>(null);
  protected readonly reductionPlan = signal<SqliteSafeFlattenPlan | null>(null);
  protected readonly receipt = signal<ActionReceiptView | null>(null);
  protected readonly selectedTimelineEntry = signal<SqliteTimelineEntry | null>(null);

  constructor() {
    effect(() => {
      const query = this.timelineQuery();
      if (query !== null) untracked(() => this.openTimeline(query));
    });
  }

  protected trackAction = (_index: number, action: SqliteRecoveryAction): string =>
    action.action_id;
  protected trackTimeline = (_index: number, entry: SqliteTimelineEntry): number =>
    entry.sequence;

  protected updateTimelineFilter(
    field: keyof Omit<SqliteTimelineQuery, 'cursor' | 'pageSize'>,
    event: Event,
  ): void {
    if (!(event.target instanceof HTMLInputElement)) return;
    const trimmed = event.target.value.trim();
    if (field === 'sequence') {
      const sequence = Number(trimmed);
      this.timelineFilters.update((current) => ({
        ...current,
        sequence: Number.isInteger(sequence) && sequence > 0 ? sequence : undefined,
      }));
      return;
    }
    this.timelineFilters.update((current) => ({
      ...current,
      [field]: trimmed === '' ? undefined : trimmed,
    }));
  }

  protected applyTimelineFilters(): void {
    this.openTimeline(this.timelineFilters());
  }

  protected clearTimelineFilters(): void {
    this.timelineFilters.set({});
    this.openTimeline({});
  }

  protected selectTimelineEntry(entry: SqliteTimelineEntry): void {
    this.selectedTimelineEntry.set(entry);
  }

  protected async runAction(action: SqliteRecoveryAction): Promise<void> {
    if (!action.available || this.busyActionId() !== null) return;
    if (action.action_id === 'open_custody_timeline') {
      this.openTimeline();
      return;
    }
    if (action.action_id === 'prepare_safe_flatten') {
      await this.prepareSafeFlatten(action);
      return;
    }
    if (!action.mutation) {
      this.actionNotice.set(action.next_step);
      return;
    }
    if (action.confirmation !== null) {
      this.confirmationAction.set(action);
      this.confirmationTarget.set(this.newCommandTarget());
      return;
    }
    await this.executeAction(action, this.newCommandTarget());
  }

  protected confirmAction(): void {
    const action = this.confirmationAction();
    const target = this.confirmationTarget();
    this.confirmationAction.set(null);
    this.confirmationTarget.set(null);
    if (action !== null && target !== null) void this.executeAction(action, target);
  }

  protected dismissReceipt(): void {
    this.receipt.set(null);
  }

  private async executeAction(action: SqliteRecoveryAction, target: ResourceTarget): Promise<void> {
    this.busyActionId.set(action.action_id);
    this.actionNotice.set(null);
    this.actionProblem.set(null);
    try {
      const receipt = await this.brokers.executeSqliteRecoveryAction(
        target,
        action,
      );
      this.receipt.set({
        actionId: action.action_id,
        outcome: 'success',
        receiptId: receipt.receipt_id,
        recordedAtMs: receipt.recorded_at_ms,
        message: receipt.applied
          ? `${action.label} completed.`
          : `${action.label} had already completed; the durable result was replayed.`,
        remediation: null,
      });
    } catch (error) {
      this.actionProblem.set(
        actionProblem(error, 'The Account Clerk could not complete this action.'),
      );
    } finally {
      this.refreshVisibleProjection();
      this.busyActionId.set(null);
    }
  }

  private async prepareSafeFlatten(action: SqliteRecoveryAction): Promise<void> {
    const target = this.target();
    const accountId = this.accountId();
    const provenance = this.provenanceKey(target, accountId);
    this.busyActionId.set(action.action_id);
    this.actionNotice.set(null);
    this.actionProblem.set(null);
    this.reductionPlan.set(null);
    try {
      const refreshed = await this.brokers.checkSqliteRecoveryAction(
        target.clerkId,
        accountId,
        action,
      );
      if (this.isCurrentProvenance(provenance)) {
        this.reductionPlan.set(refreshed.reduction_plan);
        this.actionNotice.set(refreshed.next_step);
      }
    } catch (error) {
      if (this.isCurrentProvenance(provenance)) {
        this.actionProblem.set(
          actionProblem(error, 'The Account Clerk could not prepare a safe-flatten plan.'),
        );
      }
    } finally {
      this.refreshVisibleProjection();
      this.busyActionId.set(null);
    }
  }

  protected async loadMoreTimeline(): Promise<void> {
    const cursor = this.timelineNextCursor();
    if (cursor === null || this.timelineLoadingMore()) return;
    const target = this.target();
    const accountId = this.accountId();
    const provenance = this.provenanceKey(target, accountId);
    this.timelineLoadingMoreProvenance.set(provenance);
    try {
      const page = await this.brokers.getSqliteClerkTimeline(target.clerkId, accountId, {
        ...this.timelineAppliedFilters(),
        cursor,
      });
      if (this.isCurrentProvenance(provenance)) {
        this.timeline.update((current) => [...current, ...page.entries]);
        this.timelineNextCursor.set(page.next_cursor);
        this.timelineTotalEntries.set(page.total_entries);
        this.actionNotice.set(null);
      }
    } catch {
      if (this.isCurrentProvenance(provenance)) {
        this.actionNotice.set('The custody timeline is temporarily unavailable.');
      }
    } finally {
      if (this.isCurrentProvenance(provenance)) {
        this.timelineLoadingMoreProvenance.set(null);
      }
    }
  }

  private openTimeline(query: SqliteTimelineQuery = this.timelineFilters()): void {
    this.timelineOpen.set(true);
    this.queuedTimelineQuery.set(query);
    void this.loadQueuedTimeline();
  }

  private async loadQueuedTimeline(): Promise<void> {
    if (this.timelineLoading()) return;
    const query = this.queuedTimelineQuery();
    if (query === null) return;
    this.queuedTimelineQuery.set(null);
    const target = this.target();
    const accountId = this.accountId();
    const provenance = this.provenanceKey(target, accountId);
    this.timelineLoadingProvenance.set(provenance);
    const { cursor: _cursor, pageSize: _pageSize, ...filters } = query;
    this.timelineFilters.set(filters);
    this.timelineAppliedFilters.set(filters);
    this.selectedTimelineEntry.set(null);
    try {
      const page = await this.brokers.getSqliteClerkTimeline(
        target.clerkId,
        accountId,
        filters,
      );
      if (this.isCurrentProvenance(provenance)) {
        this.timeline.set(page.entries);
        this.timelineNextCursor.set(page.next_cursor);
        this.timelineTotalEntries.set(page.total_entries);
        this.selectedTimelineEntry.set(page.entries[0] ?? null);
        this.actionNotice.set(null);
      }
    } catch {
      if (this.isCurrentProvenance(provenance)) {
        this.actionNotice.set('The custody timeline is temporarily unavailable.');
      }
    } finally {
      if (this.isCurrentProvenance(provenance)) {
        this.timelineLoadingProvenance.set(null);
        if (this.queuedTimelineQuery() !== null) void this.loadQueuedTimeline();
      }
    }
  }

  private refreshVisibleProjection(): void {
    this.projection.reload();
    this.projectionInvalidated.emit();
  }

  private newCommandTarget(): ResourceTarget {
    const target = this.target();
    if (typeof globalThis.crypto?.randomUUID !== 'function') {
      throw new Error('This browser cannot create a durable request identity.');
    }
    return withCommand(
      withAccount(target, this.accountId()),
      'custody_command',
      globalThis.crypto.randomUUID(),
    );
  }

  private provenanceKey(target: ResourceTarget, accountId: string): string {
    return laneKey(
      target.broker, target.clerkId, target.routingEpoch, target.bindingGeneration, accountId,
    );
  }

  private isCurrentProvenance(provenance: string): boolean {
    return this.currentProvenance() === provenance;
  }

  private currentProvenance(): string {
    return this.provenanceKey(this.target(), this.accountId());
  }
}
