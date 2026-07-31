import {
  ChangeDetectionStrategy,
  Component,
  DestroyRef,
  computed,
  inject,
  input,
  resource,
  signal,
} from '@angular/core';

import type { ChartHistoryPreset, PanelAction } from '../lib/broker-v2-panel.types';
import { BrokerV2PanelService } from '../lib/broker-v2-panel.service';
import { TraderLensComponent } from '../trader-lens/trader-lens.component';
import { OperatorLensComponent } from '../operator-lens/operator-lens.component';

/**
 * Panel shell — host for all bot control panel lenses (spec §3, §6, §7).
 *
 * ## Shell responsibilities
 * - Route parameter extraction (broker, accountId, sid).
 * - Data loading: panel view (5s poll), live chart (5s poll), history chart
 *   (on preset change).
 * - Action execution (post to backend, re-poll on success).
 *
 * ## Lens architecture (S3 trader + S4 operator)
 * The `activeLens` signal determines which lens renders. The tab bar in the
 * template drives `selectLens()`. Both lenses receive identical `panel` +
 * `profile` + `actionPending` inputs from the shell.
 *
 * The operator lens additionally receives `broker`, `accountId`, and `sid`
 * so it can call the operator-gated evidence endpoint directly.
 */
@Component({
  selector: 'app-bot-panel-shell',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [TraderLensComponent, OperatorLensComponent],
  templateUrl: './bot-panel-shell.component.html',
  styleUrl: './bot-panel-shell.component.scss',
})
export class BotPanelShellComponent {
  // ── Route inputs (Angular route input binding) ────────────────────────────

  readonly broker = input.required<string>();
  readonly accountId = input.required<string>();
  readonly sid = input.required<string>();

  // ── Services ──────────────────────────────────────────────────────────────

  private readonly panelSvc = inject(BrokerV2PanelService);
  private readonly destroyRef = inject(DestroyRef);

  // ── Active lens ──────────────────────────────────────────────────────────
  // Reads the `?lens=` query param if provided; defaults to 'trader'.
  // Set via selectLens() or the tab toggle in the template.

  protected readonly activeLens = signal<'trader' | 'operator'>('trader');

  // ── Internal state ────────────────────────────────────────────────────────

  protected readonly selectedPreset = signal<ChartHistoryPreset>('1D');
  protected readonly actionPending = signal(false);
  private readonly actionError = signal<string | null>(null);

  protected readonly panel = resource({
    params: () => this.routeParams(),
    loader: ({ params }) =>
      this.panelSvc.getPanel(params.broker, params.accountId, params.sid),
  });

  protected readonly profile = resource({
    params: () => this.broker(),
    loader: ({ params }) => this.panelSvc.getPanelProfile(params),
  });

  protected readonly liveChart = resource({
    params: () => this.routeParams(),
    loader: ({ params }) =>
      this.panelSvc.getLiveChart(params.broker, params.accountId, params.sid),
  });

  protected readonly histChart = resource({
    params: () => ({ ...this.routeParams(), preset: this.selectedPreset() }),
    loader: ({ params }) =>
      this.panelSvc.getHistoryChart(
        params.broker,
        params.accountId,
        params.sid,
        params.preset,
      ),
  });

  protected readonly isLoaded = computed(
    () => this.panel.hasValue() && this.profile.hasValue(),
  );

  protected readonly loadError = computed(() => {
    if (this.actionError()) return this.actionError();
    const error = this.panel.error() ?? this.profile.error();
    if (error === undefined) return null;
    return error instanceof Error ? error.message : 'Failed to load panel data.';
  });

  // ── Lifecycle ─────────────────────────────────────────────────────────────

  constructor() {
    const pollTimer = setInterval(() => {
      this.panel.reload();
      this.liveChart.reload();
    }, 5_000);
    this.destroyRef.onDestroy(() => clearInterval(pollTimer));
  }

  // ── Shell helpers for S4 extension ───────────────────────────────────────

  /** Called by the tab bar to switch between lenses. */
  protected selectLens(lens: 'trader' | 'operator'): void {
    this.activeLens.set(lens);
  }

  private routeParams(): {
    broker: string;
    accountId: string;
    sid: string;
  } {
    return {
      broker: this.broker(),
      accountId: this.accountId(),
      sid: this.sid(),
    };
  }

  // ── Template handlers ─────────────────────────────────────────────────────

  protected onPresetChange(preset: ChartHistoryPreset): void {
    this.selectedPreset.set(preset);
  }

  protected async onActionRequested(action: PanelAction): Promise<void> {
    if (this.actionPending()) return;
    this.actionPending.set(true);
    this.actionError.set(null);
    try {
      await this.panelSvc.runBotAction(
        this.broker(),
        this.accountId(),
        this.sid(),
        action,
      );
      this.panel.reload();
    } catch (error) {
      this.actionError.set(
        error instanceof Error ? error.message : `Action "${action.label}" failed.`,
      );
    } finally {
      this.actionPending.set(false);
    }
  }
}
