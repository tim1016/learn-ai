import {
  ChangeDetectionStrategy,
  Component,
  DestroyRef,
  computed,
  effect,
  inject,
  input,
  output,
  resource,
} from '@angular/core';
import { DOCUMENT } from '@angular/common';
import { RouterLink } from '@angular/router';

import { AssetIdentityComponent } from '../../../../shared/asset-identity/asset-identity.component';
import {
  ReceiptLabelPipe,
  formatReceiptLabel,
} from '../../../../shared/pipes/receipt-label.pipe';
import { TimestampDisplayComponent } from '../../../../shared/timestamp/timestamp-display.component';
import { fmtExposure, fmtInteger, fmtSignedCurrency } from '../../format';
import { PanelActionButtonComponent } from '../panel-action-button/panel-action-button.component';
import { actionTone } from '../bot-detail-banner/lifecycle-action';
import { TriageActivityComponent } from './triage-activity.component';
import { TriageEvidenceComponent } from './triage-evidence.component';
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
    PanelActionButtonComponent,
    AssetIdentityComponent,
    TriageActivityComponent,
    TriageEvidenceComponent,
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
  /** Bump to refetch after an action lands on the selected bot. */
  readonly refreshToken = input(0);
  readonly actionTriggered = output<PanelActionTrigger>();

  private readonly panelService = inject(BrokerV2PanelService);
  private readonly destroyRef = inject(DestroyRef);
  private readonly document = inject(DOCUMENT);

  /**
   * Identity of the selected bot, and nothing else — `undefined` parks both
   * resources until one is selected.
   *
   * Refreshes deliberately do NOT ride in here. Angular treats a params change
   * as a *new* request: the resource drops its previous value, `view()` goes
   * null, and the whole pane is replaced by the loading placeholder. That is
   * correct when the selected bot changes and wrong for a refresh of the bot
   * already on screen, which would otherwise flicker the pane — and tear down
   * any open action confirmation — on every poll. Refreshes call `reload()`,
   * which keeps the current value visible while the request is in flight.
   */
  private readonly selection = computed(() => {
    const sid = this.sid();
    return sid === null
      ? undefined
      : { broker: this.broker(), accountId: this.accountId(), sid };
  });

  protected readonly panel = resource({
    params: this.selection,
    loader: ({ params }) =>
      this.panelService.getPanel(params.broker, params.accountId, params.sid),
  });

  protected readonly journal = resource({
    params: this.selection,
    loader: ({ params }) =>
      this.panelService.getEvidence(params.broker, params.accountId, params.sid, {
        pageSize: JOURNAL_PAGE_SIZE,
      }),
  });

  constructor() {
    // The per-bot panel route streams its panel over SSE. This pane is a
    // summary, so it re-reads on a timer instead of standing up a second
    // subscription — enough that a diagnosis cannot sit stale while the rail
    // beside it keeps updating.
    //
    // The panel projection ALONE. Evidence is never polled:
    // `read_evidence_page` appends one `EvidenceAuditEntry` per call, stamped
    // with `operator_identity` and `read_at_ms` — the record asserts that an
    // operator read this bot's raw evidence at that instant. Nothing rotates
    // or prunes `evidence_audit.jsonl`; `_append_audit_entry` is its only
    // writer. A background poll would forge ~240 of those assertions an hour
    // per selected bot and bury the genuine reads, so evidence is read only on
    // a real operator act: selecting a bot, retrying, or acting on one.
    const timer = setInterval(() => {
      if (this.document.visibilityState === 'visible') this.panel.reload();
    }, PANEL_POLL_MS);
    this.destroyRef.onDestroy(() => clearInterval(timer));

    // An action landed on the selected bot: re-read both projections. This is
    // a genuine operator act, so the evidence read is legitimately audited.
    let lastToken = this.refreshToken();
    effect(() => {
      const token = this.refreshToken();
      if (token === lastToken) return;
      lastToken = token;
      this.panel.reload();
      this.journal.reload();
    });
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

  /**
   * Per-command availability, unavailable first.
   *
   * These are NOT pass/fail admission gates: the backend emits one check per
   * panel action with `ready = action.enabled`, and the lifecycle commands are
   * mutually exclusive, so a healthy running bot legitimately reports Resume
   * and Continue as unavailable. Unavailable-first answers the question the
   * card exists for — "why is that command greyed out?".
   */
  protected readonly commands = computed<readonly ReadinessCheckView[]>(() =>
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
        value: formatReceiptLabel(pulse.feed_state),
        tone: pulse.attention_required ? 'warn' : 'positive',
      },
    ];
  });

  /**
   * A failed evidence read is not an empty journal. `JournalTailComponent`
   * renders "No journal entries." for a null page, which would turn an
   * unavailable custody read into an affirmative empty-audit claim.
   */
  protected readonly journalFailed = computed(() => this.journal.error() !== undefined);

  protected readonly actionTone = actionTone;

  protected reload(): void {
    this.panel.reload();
    this.journal.reload();
  }

  protected onActionTriggered(trigger: PanelActionTrigger): void {
    this.actionTriggered.emit(trigger);
  }

  protected pnlTone(value: number | null | undefined): Tone {
    if (value === null || value === undefined || value === 0) return 'muted';
    return value > 0 ? 'positive' : 'negative';
  }
}
