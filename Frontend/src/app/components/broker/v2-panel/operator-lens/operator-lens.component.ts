import {
  ChangeDetectionStrategy,
  Component,
  DestroyRef,
  computed,
  effect,
  inject,
  input,
  output,
  signal,
} from '@angular/core';
import type {
  BotPanelView,
  EvidencePage,
  PanelAction,
  PanelProfile,
} from '../lib/broker-v2-panel.types';
import { BrokerV2PanelService } from '../lib/broker-v2-panel.service';
import { TransactionRailComponent } from './transaction-rail.component';
import { HealthCardComponent } from './health-card.component';
import { ClerkCardComponent } from './clerk-card.component';
import { JournalTailComponent } from './journal-tail.component';
import { EvidenceDrawerComponent } from './evidence-drawer.component';
import { PanelActionButtonComponent } from '../panel-action-button/panel-action-button.component';

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
    PanelActionButtonComponent,
  ],
  templateUrl: './operator-lens.component.html',
  styleUrl: './operator-lens.component.scss',
})
export class OperatorLensComponent {
  // ── Shell-provided data ───────────────────────────────────────────────────

  readonly panel = input.required<BotPanelView>();
  readonly profile = input.required<PanelProfile>();
  readonly actionPending = input(false);

  readonly actionRequested = output<PanelAction>();

  // Route context — needed for the evidence endpoint calls.
  readonly broker = input.required<string>();
  readonly accountId = input.required<string>();
  readonly sid = input.required<string>();

  // ── Services ──────────────────────────────────────────────────────────────

  private readonly panelSvc = inject(BrokerV2PanelService);
  private readonly destroyRef = inject(DestroyRef);

  // ── Local state ───────────────────────────────────────────────────────────

  /** Currently selected transaction_ref (drives the rail). */
  protected readonly selectedTransactionRef = signal<string | null>(null);

  /** Whether the evidence drawer is open. */
  protected readonly evidenceDrawerOpen = signal(false);

  /** The transaction_ref whose evidence the drawer is showing. */
  protected readonly evidenceDrawerRef = signal<string | null>(null);

  /** Journal tail evidence page (operator-gated endpoint). */
  protected readonly journalPage = signal<EvidencePage | null>(null);
  protected readonly journalLoading = signal(false);
  protected readonly journalError = signal<string | null>(null);

  // ── Derived ──────────────────────────────────────────────────────────────

  protected readonly health = computed(() => this.panel().health);
  protected readonly clerk = computed(() => this.panel().clerk);
  protected readonly rail = computed(() => this.panel().rail);

  /** Operator-lens actions (operator-relevant subset). */
  protected readonly retireAction = computed<PanelAction | null>(
    () => this.panel().actions.find((a) => a.action_id === 'retire') ?? null,
  );

  protected readonly reconcileAction = computed<PanelAction | null>(
    () => this.panel().actions.find((a) => a.action_id === 'reconcile_now') ?? null,
  );

  protected readonly clearHoldAction = computed<PanelAction | null>(
    () => this.panel().actions.find((a) => a.action_id === 'clear_hold') ?? null,
  );

  protected readonly flattenStopAction = computed<PanelAction | null>(
    () => this.panel().actions.find((a) => a.action_id === 'flatten_stop') ?? null,
  );

  /**
   * The rail to render: if the user has selected a different transaction via
   * the journal tail, we override the rail's transaction_ref locally.
   *
   * For S4 the rail itself is still the full server-provided one (the server
   * selects the most-recent transaction). A future slice can make the panel
   * poll with `?transaction_ref=` once the user selects a different row.
   */
  protected readonly displayRail = computed(() => {
    const ref = this.selectedTransactionRef();
    const serverRail = this.rail();
    if (!ref) return serverRail;
    // Re-use the server rail; the transaction_ref override drives what the
    // user "thinks" is selected. Full per-transaction rail switching is a
    // panel-poll concern (poll with ?transaction_ref=); mark the current
    // selection for visual feedback only.
    return { ...serverRail, transaction_ref: ref };
  });

  constructor() {
    this.destroyRef.onDestroy(() => {
      // Nothing to clean up (no timers — the shell owns the poll cycle).
    });

    // Load the journal tail when the panel loads (bot + account known).
    effect(() => {
      const panelData = this.panel();
      if (!panelData) return;
      void this.loadJournalPage();
    });
  }

  // ── Template handlers ─────────────────────────────────────────────────────

  protected onTransactionSelected(ref: string): void {
    this.selectedTransactionRef.set(ref);
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

  // ── Private ───────────────────────────────────────────────────────────────

  private async loadJournalPage(): Promise<void> {
    this.journalLoading.set(true);
    this.journalError.set(null);
    try {
      const page = await this.panelSvc.getEvidence(
        this.broker(),
        this.accountId(),
        this.sid(),
        { clientHint: 'operator-lens-journal-tail' },
      );
      this.journalPage.set(page);
    } catch (error) {
      this.journalError.set(
        error instanceof Error
          ? error.message
          : 'Could not load journal evidence.',
      );
    } finally {
      this.journalLoading.set(false);
    }
  }
}
