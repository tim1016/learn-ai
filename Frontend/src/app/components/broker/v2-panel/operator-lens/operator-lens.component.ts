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
import type {
  BotPanelView,
  PanelAction,
  PanelActionTrigger,
  PanelProfile,
} from '../lib/broker-v2-panel.types';
import type { TickerQuoteView } from '../../../../shared/ticker-quote/ticker-quote.component';
import { BrokerV2PanelService } from '../lib/broker-v2-panel.service';
import { TransactionRailComponent } from './transaction-rail.component';
import { HealthCardComponent } from './health-card.component';
import { ClerkCardComponent } from './clerk-card.component';
import { JournalTailComponent } from './journal-tail.component';
import { OperatorReadinessComponent } from './operator-readiness.component';
import { ReceiptLabelPipe } from '../../../../shared/pipes/receipt-label.pipe';
import { AssetIdentityComponent } from '../../../../shared/asset-identity';
import { OperatorRunHistoryComponent } from '../bot-run-history/operator-run-history.component';
import { OperatorDisclosureCardComponent } from './operator-disclosure-card.component';
import { OperatorBotBannerComponent } from './operator-bot-banner/operator-bot-banner.component';
import { primaryActionForLens } from '../bot-detail-banner/lifecycle-action';

/**
 * Operator lens (spec §7).
 *
 * Orchestrates the four operator-lens regions:
 * - Transaction rail (center): six-station pipeline for one selected transaction.
 * - Health card (beside rail): phase, duty outcome, decision freshness, Retire.
 * - Clerk card (beside rail): hold state, reconciliation, channels, Reconcile/Clear hold.
 * - Journal tail (bottom): newest-first, filterable, selects rail transaction.
 * Raw transaction evidence is loaded on demand inside the station accordion.
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
    OperatorReadinessComponent,
    ReceiptLabelPipe,
    OperatorRunHistoryComponent,
    OperatorDisclosureCardComponent,
    AssetIdentityComponent,
    OperatorBotBannerComponent,
  ],
  templateUrl: './operator-lens.component.html',
  styleUrl: './operator-lens.component.scss',
})
export class OperatorLensComponent {
  // ── Shell-provided data ───────────────────────────────────────────────────

  readonly panel = input.required<BotPanelView>();
  readonly tickerQuote = input<TickerQuoteView | null>(null);
  readonly profile = input.required<PanelProfile>();
  readonly actionPending = input(false);

  readonly actionRequested = output<PanelActionTrigger>();
  readonly transactionSelected = output<string>();

  // Route context — needed for the evidence endpoint calls.
  readonly broker = input.required<string>();
  readonly accountId = input.required<string>();
  readonly sid = input.required<string>();

  // ── Services ──────────────────────────────────────────────────────────────

  private readonly panelSvc = inject(BrokerV2PanelService);

  // ── Local state ───────────────────────────────────────────────────────────

  /** Audit evidence activates on first open, then reloads only for a new server cursor. */
  protected readonly journalActivated = signal(false);

  /** Reloads only when the journal cursor changes; panel polling alone is inert. */
  protected readonly journalPage = resource({
    params: () => this.journalActivated()
      ? [
          this.broker(),
          this.accountId(),
          this.sid(),
          this.panel().journal_tail_seq ?? 'empty',
        ].join('|')
      : undefined,
    loader: () =>
      this.panelSvc.getEvidence(this.broker(), this.accountId(), this.sid(), {
        pageSize: 24,
        clientHint: 'operator-lens-journal-tail',
      }),
  });

  // ── Derived ──────────────────────────────────────────────────────────────

  protected readonly health = computed(() => this.panel().health);
  protected readonly clerk = computed(() => this.panel().clerk);
  protected readonly rail = computed(() => this.panel().rail);
  protected readonly primaryAction = computed<PanelAction | null>(() =>
    primaryActionForLens(this.panel(), 'operator'),
  );

  // ── Template handlers ─────────────────────────────────────────────────────

  protected onTransactionSelected(ref: string): void {
    this.transactionSelected.emit(ref);
  }

  protected onJournalExpanded(expanded: boolean): void {
    if (expanded) this.journalActivated.set(true);
  }

  protected onActionRequested(action: PanelActionTrigger): void {
    this.actionRequested.emit(action);
  }

}
