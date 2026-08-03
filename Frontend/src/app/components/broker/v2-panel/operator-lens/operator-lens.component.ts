import {
  ChangeDetectionStrategy,
  Component,
  computed,
  inject,
  input,
  output,
  resource,
  signal,
} from '@angular/core';
import { EMPTY_RUN_HISTORY_STATE } from '../lib/broker-v2-panel.types';
import type {
  BotPanelView,
  PanelAction,
  PanelProfile,
  RunHistoryNavigation,
  RunHistoryState,
} from '../lib/broker-v2-panel.types';
import { BrokerV2PanelService } from '../lib/broker-v2-panel.service';
import { TransactionRailComponent } from './transaction-rail.component';
import { HealthCardComponent } from './health-card.component';
import { ClerkCardComponent } from './clerk-card.component';
import { JournalTailComponent } from './journal-tail.component';
import { EvidenceDrawerComponent } from './evidence-drawer.component';
import { OperatorReadinessComponent } from './operator-readiness.component';
import { ReceiptLabelPipe } from '../../../../shared/pipes/receipt-label.pipe';
import { BotRunHistoryComponent } from '../bot-run-history/bot-run-history.component';

/**
 * Operator lens (spec §7).
 *
 * Orchestrates the four operator-lens regions:
 * - Transaction rail (center): six-station pipeline for one selected transaction.
 * - Health card (beside rail): phase, duty outcome, decision freshness, Retire.
 * - Clerk card (beside rail): hold state, reconciliation, channels, Reconcile/Clear hold.
 * - Journal tail (bottom): newest-first, filterable, selects rail transaction.
 * - Evidence drawer: operator-gated, paged, audit-logged raw evidence.
 *
 * The shell passes `panel` and `profile` as inputs (same as the trader lens).
 * Action execution is handled by the shell's `onActionRequested` output, same as S3.
 */
@Component({
  selector: 'app-operator-lens',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [
    TransactionRailComponent,
    HealthCardComponent,
    ClerkCardComponent,
    JournalTailComponent,
    EvidenceDrawerComponent,
    OperatorReadinessComponent,
    ReceiptLabelPipe,
    BotRunHistoryComponent,
  ],
  templateUrl: './operator-lens.component.html',
  styleUrl: './operator-lens.component.scss',
})
export class OperatorLensComponent {
  // ── Shell-provided data ───────────────────────────────────────────────────

  readonly panel = input.required<BotPanelView>();
  readonly profile = input.required<PanelProfile>();
  readonly actionPending = input(false);
  readonly runHistory = input<RunHistoryState>(EMPTY_RUN_HISTORY_STATE);

  readonly actionRequested = output<PanelAction>();
  readonly transactionSelected = output<string>();
  readonly runHistoryNavigation = output<RunHistoryNavigation>();

  // Route context — needed for the evidence endpoint calls.
  readonly broker = input.required<string>();
  readonly accountId = input.required<string>();
  readonly sid = input.required<string>();

  // ── Services ──────────────────────────────────────────────────────────────

  private readonly panelSvc = inject(BrokerV2PanelService);

  // ── Local state ───────────────────────────────────────────────────────────

  /** Whether the evidence drawer is open. */
  protected readonly evidenceDrawerOpen = signal(false);

  /** The transaction_ref whose evidence the drawer is showing. */
  protected readonly evidenceDrawerRef = signal<string | null>(null);

  /** Reloads only when the journal cursor changes; panel polling alone is inert. */
  protected readonly journalPage = resource({
    params: () =>
      [
        this.broker(),
        this.accountId(),
        this.sid(),
        this.panel().journal_tail_seq ?? 'empty',
      ].join('|'),
    loader: () =>
      this.panelSvc.getEvidence(this.broker(), this.accountId(), this.sid(), {
        clientHint: 'operator-lens-journal-tail',
      }),
  });

  // ── Derived ──────────────────────────────────────────────────────────────

  protected readonly health = computed(() => this.panel().health);
  protected readonly clerk = computed(() => this.panel().clerk);
  protected readonly rail = computed(() => this.panel().rail);

  // ── Template handlers ─────────────────────────────────────────────────────

  protected onTransactionSelected(ref: string): void {
    this.transactionSelected.emit(ref);
  }

  protected onEvidenceRequested(ref: string): void {
    this.evidenceDrawerRef.set(ref);
    this.evidenceDrawerOpen.set(true);
  }

  protected onEvidenceDrawerClose(): void {
    this.evidenceDrawerOpen.set(false);
    this.evidenceDrawerRef.set(null);
  }

  protected onActionRequested(action: PanelAction): void {
    this.actionRequested.emit(action);
  }

}
