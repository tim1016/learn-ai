import {
  ChangeDetectionStrategy,
  Component,
  DestroyRef,
  computed,
  inject,
  input,
  output,
  resource,
  signal,
} from '@angular/core';
import { DOCUMENT } from '@angular/common';
import { RouterLink } from '@angular/router';

import { ReceiptLabelPipe } from '../../../../shared/pipes/receipt-label.pipe';
import { TimestampDisplayComponent } from '../../../../shared/timestamp/timestamp-display.component';
import { fmtExposure, fmtInteger, fmtSignedCurrency } from '../../format';
import { JournalTailComponent } from '../operator-lens/journal-tail.component';
import { PanelActionButtonComponent } from '../panel-action-button/panel-action-button.component';
import { BrokerV2PanelService } from '../lib/broker-v2-panel.service';
import type {
  BotPanelView,
  PanelActionTrigger,
  ReadinessCheckView,
} from '../lib/broker-v2-panel.types';

type Tone = 'positive' | 'negative' | 'warn' | 'neutral' | 'muted';

interface MetricTile {
  readonly label: string;
  readonly value: string;
  readonly tone: Tone;
}

const JOURNAL_PAGE_SIZE = 12;
const PANEL_POLL_MS = 15_000;

/**
 * Merged triage detail for the bot selected in the rail.
 *
 * This is the trade fact and the failure diagnosis in one pane — the design's
 * "one lens" claim for this screen. It is deliberately a *summary*: the
 * per-bot panel route (`/bots/:sid`) keeps the full lens split, the live
 * chart, and run history, and this pane links to it rather than duplicating
 * the chart's streaming plumbing.
 *
 * Actions render through `PanelActionButtonComponent` so backend-declared
 * confirmations and blockers keep gating them; execution is delegated upward
 * so the page stays the single action-execution owner.
 */
@Component({
  selector: 'app-bot-triage-detail',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [
    RouterLink,
    ReceiptLabelPipe,
    TimestampDisplayComponent,
    JournalTailComponent,
    PanelActionButtonComponent,
  ],
  templateUrl: './bot-triage-detail.component.html',
  styleUrl: './bot-triage-detail.component.scss',
  host: { class: 'flex min-h-0 flex-col' },
})
export class BotTriageDetailComponent {
  readonly broker = input.required<string>();
  readonly accountId = input.required<string>();
  readonly sid = input<string | null>(null);
  readonly pending = input(false);
  /** Bump to refetch after an action lands; participates in the resource params. */
  readonly refreshToken = input(0);
  readonly actionTriggered = output<PanelActionTrigger>();

  private readonly panelService = inject(BrokerV2PanelService);
  private readonly destroyRef = inject(DestroyRef);
  private readonly document = inject(DOCUMENT);

  private readonly pollTick = signal(0);

  /** `undefined` parks both resources until a bot is selected. */
  private readonly selection = computed(() => {
    const sid = this.sid();
    return sid === null
      ? undefined
      : { broker: this.broker(), accountId: this.accountId(), sid };
  });

  /**
   * The panel projection is a plain read, designed for a 5s poll cadence.
   */
  private readonly panelParams = computed(() => {
    const selection = this.selection();
    if (selection === undefined) return undefined;
    return { ...selection, token: this.refreshToken() + this.pollTick() };
  });

  /**
   * Evidence is deliberately NOT on the timer.
   *
   * `read_evidence_page` appends one `EvidenceAuditEntry` per call, stamped
   * with `operator_identity` and `read_at_ms` — the record asserts that an
   * operator read this bot's raw evidence at that instant. Nothing rotates or
   * prunes `evidence_audit.jsonl`; `_append_audit_entry` is its only writer.
   * A background poll would forge ~240 of those assertions an hour per
   * selected bot and bury the genuine reads, so this reads only on a real
   * operator action: selecting a bot, retrying, or acting on one.
   */
  private readonly journalParams = computed(() => {
    const selection = this.selection();
    if (selection === undefined) return undefined;
    return { ...selection, token: this.refreshToken() };
  });

  protected readonly panel = resource({
    params: this.panelParams,
    loader: ({ params }) =>
      this.panelService.getPanel(params.broker, params.accountId, params.sid),
  });

  protected readonly journal = resource({
    params: this.journalParams,
    loader: ({ params }) =>
      this.panelService.getEvidence(params.broker, params.accountId, params.sid, {
        pageSize: JOURNAL_PAGE_SIZE,
      }),
  });

  constructor() {
    // The per-bot panel route streams its panel over SSE. This pane is a
    // summary, so it re-reads on a timer instead of standing up a second
    // subscription — enough that a diagnosis cannot sit stale while the rail
    // beside it keeps updating. The panel projection alone: see
    // `journalParams` for why evidence must never be polled.
    const timer = setInterval(() => {
      if (this.document.visibilityState === 'visible' && !this.panel.isLoading()) {
        this.pollTick.update((tick) => tick + 1);
      }
    }, PANEL_POLL_MS);
    this.destroyRef.onDestroy(() => clearInterval(timer));
  }

  /** Guards against rendering the previous bot's panel while the next one loads. */
  protected readonly view = computed<BotPanelView | null>(() => {
    const value = this.panel.hasValue() ? this.panel.value() : null;
    return value !== null && value.strategy_instance_id === this.sid() ? value : null;
  });

  protected readonly panelUnavailable = computed(
    () => this.panel.error() !== undefined && this.view() === null,
  );

  protected readonly botLink = computed(() => [
    '/brokers',
    this.broker(),
    'accounts',
    this.accountId(),
    'bots',
    this.sid() ?? '',
  ]);

  protected readonly verdictTone = computed<Tone>(() => {
    const state = this.view()?.mission_verdict.state;
    if (state === 'blocked') return 'negative';
    if (state === 'working' || state === 'ready') return 'positive';
    return 'muted';
  });

  private readonly gateTotal = computed(() => {
    const view = this.view();
    if (view === null) return 0;
    return view.readiness_ready_count + view.readiness_blocked_count;
  });

  /** Blocked gates first — the pane exists to answer "why is this bot stuck?". */
  protected readonly gates = computed<readonly ReadinessCheckView[]>(() =>
    [...(this.view()?.readiness_checks ?? [])].sort(
      (left, right) => Number(left.ready) - Number(right.ready),
    ),
  );

  protected readonly metrics = computed<readonly MetricTile[]>(() => {
    const view = this.view();
    if (view === null) return [];

    const realized = view.realized_pnl_today;
    const exposure = fmtExposure(view.exposure);
    const pulse = view.market_pulse;

    return [
      {
        label: 'Gates',
        value: `${view.readiness_ready_count} / ${this.gateTotal()}`,
        tone: view.readiness_blocked_count > 0 ? 'negative' : 'positive',
      },
      { label: 'Exposure', value: exposure, tone: exposure === 'Flat' ? 'muted' : 'neutral' },
      {
        label: 'Realized',
        value: fmtSignedCurrency(realized),
        tone: this.pnlTone(realized),
      },
      { label: 'Fills today', value: fmtInteger(view.fills_today), tone: 'neutral' },
      {
        label: 'Runner',
        value: view.health.phase_label,
        tone: view.health.running ? 'positive' : 'muted',
      },
      {
        label: 'Data feed',
        value: pulse.feed_state,
        tone: pulse.attention_required ? 'warn' : 'positive',
      },
    ];
  });

  protected reload(): void {
    this.panel.reload();
    this.journal.reload();
  }

  protected onActionTriggered(trigger: PanelActionTrigger): void {
    this.actionTriggered.emit(trigger);
  }

  protected pnlTone(value: number | null | undefined): Tone {
    if (value == null || value === 0) return 'muted';
    return value > 0 ? 'positive' : 'negative';
  }
}
