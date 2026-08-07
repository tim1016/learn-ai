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
} from '@angular/core';

import { ReceiptLabelPipe } from '../../../shared/pipes/receipt-label.pipe';
import { TimestampDisplayComponent } from '../../../shared/timestamp';
import { BrokersService } from '../../../services/brokers.service';
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

/** Existing Alpaca Desk adapter for the boot-selected SQLite Clerk authority. */
@Component({
  selector: 'app-alpaca-sqlite-custody',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [
    PanelActionReceiptComponent,
    ReceiptLabelPipe,
    SafeFlattenPlanComponent,
    TimestampDisplayComponent,
    TypedHaltConfirmComponent,
  ],
  templateUrl: './alpaca-sqlite-custody.component.html',
  styleUrl: './alpaca-sqlite-custody.component.scss',
})
export class AlpacaSqliteCustodyComponent {
  readonly accountId = input.required<string>();
  readonly legacyAuthorityChanged = output<boolean>();
  private readonly brokers = inject(BrokersService);

  protected readonly projection = resource({
    params: () => this.accountId(),
    loader: ({ params }) => this.brokers.getSqliteClerkProjection(params),
  });
  protected readonly timeline = signal<readonly SqliteTimelineEntry[]>([]);
  protected readonly timelineOpen = signal(false);
  protected readonly timelineLoading = signal(false);
  protected readonly timelineLoadingMore = signal(false);
  protected readonly timelineNextCursor = signal<string | null>(null);
  protected readonly timelineTotalEntries = signal(0);
  protected readonly busyActionId = signal<string | null>(null);
  protected readonly actionNotice = signal<string | null>(null);
  protected readonly confirmationAction = signal<SqliteRecoveryAction | null>(null);
  protected readonly reductionPlan = signal<SqliteSafeFlattenPlan | null>(null);
  protected readonly receipt = signal<ActionReceiptView | null>(null);
  protected readonly isLegacyAuthority = computed(() => {
    const error = this.projection.error();
    return error instanceof HttpErrorResponse && error.status === 409;
  });

  constructor() {
    effect(() => {
      if (this.projection.hasValue()) {
        this.legacyAuthorityChanged.emit(false);
      } else if (this.projection.error() !== undefined) {
        this.legacyAuthorityChanged.emit(this.isLegacyAuthority());
      }
    });
  }

  protected trackAction = (_index: number, action: SqliteRecoveryAction): string =>
    action.action_id;
  protected trackTimeline = (_index: number, entry: SqliteTimelineEntry): number =>
    entry.sequence;

  protected async runAction(action: SqliteRecoveryAction): Promise<void> {
    if (!action.available || this.busyActionId() !== null) return;
    if (action.action_id === 'open_custody_timeline') {
      await this.openTimeline();
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
      return;
    }
    await this.executeAction(action);
  }

  protected confirmAction(): void {
    const action = this.confirmationAction();
    this.confirmationAction.set(null);
    if (action !== null) void this.executeAction(action);
  }

  protected dismissReceipt(): void {
    this.receipt.set(null);
  }

  private async executeAction(action: SqliteRecoveryAction): Promise<void> {
    this.busyActionId.set(action.action_id);
    this.actionNotice.set(null);
    try {
      const receipt = await this.brokers.executeSqliteRecoveryAction(
        this.accountId(),
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
      this.projection.reload();
    } catch (error) {
      this.actionNotice.set(
        error instanceof HttpErrorResponse && error.status === 409
          ? 'Clerk evidence changed. Review the refreshed action before trying again.'
          : 'The Account Clerk could not complete this action.',
      );
      this.projection.reload();
    } finally {
      this.busyActionId.set(null);
    }
  }

  private async prepareSafeFlatten(action: SqliteRecoveryAction): Promise<void> {
    this.busyActionId.set(action.action_id);
    this.actionNotice.set(null);
    this.reductionPlan.set(null);
    try {
      const refreshed = await this.brokers.checkSqliteRecoveryAction(
        this.accountId(),
        action,
      );
      this.reductionPlan.set(refreshed.reduction_plan);
      this.actionNotice.set(refreshed.next_step);
    } catch (error) {
      this.actionNotice.set(
        error instanceof HttpErrorResponse && error.status === 409
          ? 'Clerk evidence changed. Review the refreshed action before trying again.'
          : 'The Account Clerk could not prepare a safe-flatten plan.',
      );
      this.projection.reload();
    } finally {
      this.busyActionId.set(null);
    }
  }

  protected async loadMoreTimeline(): Promise<void> {
    const cursor = this.timelineNextCursor();
    if (cursor === null || this.timelineLoadingMore()) return;
    this.timelineLoadingMore.set(true);
    try {
      const page = await this.brokers.getSqliteClerkTimeline(this.accountId(), cursor);
      this.timeline.update((current) => [...current, ...page.entries]);
      this.timelineNextCursor.set(page.next_cursor);
      this.timelineTotalEntries.set(page.total_entries);
      this.actionNotice.set(null);
    } catch {
      this.actionNotice.set('The custody timeline is temporarily unavailable.');
    } finally {
      this.timelineLoadingMore.set(false);
    }
  }

  private async openTimeline(): Promise<void> {
    this.timelineOpen.set(true);
    if (this.timelineLoading()) return;
    this.timelineLoading.set(true);
    try {
      const page = await this.brokers.getSqliteClerkTimeline(this.accountId());
      this.timeline.set(page.entries);
      this.timelineNextCursor.set(page.next_cursor);
      this.timelineTotalEntries.set(page.total_entries);
      this.actionNotice.set(null);
    } catch {
      this.actionNotice.set('The custody timeline is temporarily unavailable.');
    } finally {
      this.timelineLoading.set(false);
    }
  }
}
