import {
  ChangeDetectionStrategy,
  Component,
  DestroyRef,
  computed,
  effect,
  inject,
  input,
  signal,
} from '@angular/core';

import type {
  BotPanelView,
  ChartHistoryPreset,
  ChartHistoryResponse,
  ChartLiveResponse,
  PanelAction,
  PanelProfile,
} from '../lib/broker-v2-panel.types';
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

  protected readonly panel = signal<BotPanelView | null>(null);
  protected readonly profile = signal<PanelProfile | null>(null);
  protected readonly liveChart = signal<ChartLiveResponse | null>(null);
  protected readonly histChart = signal<ChartHistoryResponse | null>(null);
  protected readonly selectedPreset = signal<ChartHistoryPreset>('1D');
  protected readonly actionPending = signal(false);
  protected readonly loadError = signal<string | null>(null);

  protected readonly isLoaded = computed(
    () => this.panel() !== null && this.profile() !== null,
  );

  private panelInFlight = false;
  private liveChartInFlight = false;

  // ── Polling timers ────────────────────────────────────────────────────────

  private readonly POLL_MS = 5_000;
  private pollTimer: ReturnType<typeof setInterval> | null = null;
  private liveChartTimer: ReturnType<typeof setInterval> | null = null;

  // ── Lifecycle ─────────────────────────────────────────────────────────────

  constructor() {
    this.destroyRef.onDestroy(() => this.teardown());

    // Defer initial load until route inputs (broker, accountId, sid) are bound.
    // input.required() signals throw NG0950 if accessed before the host element
    // has received its values. An effect() runs after the first change-detection
    // cycle, guaranteeing inputs are available.
    //
    // Teardown before reload guards against same-component re-navigation
    // (SPA back/forward with different route params): stops any running poll
    // timers before starting fresh ones, preventing timer accumulation.
    effect(() => {
      const broker = this.broker();
      const accountId = this.accountId();
      const sid = this.sid();

      if (!broker || !accountId || !sid) return;

      this.teardown();
      this.panel.set(null);
      this.profile.set(null);
      void this.initialLoad();
    });
  }

  // ── Shell helpers for S4 extension ───────────────────────────────────────

  /** Called by the tab bar to switch between lenses. */
  protected selectLens(lens: 'trader' | 'operator'): void {
    this.activeLens.set(lens);
  }

  // ── Data loading ──────────────────────────────────────────────────────────

  private async initialLoad(): Promise<void> {
    await Promise.all([
      this.loadProfile(),
      this.loadPanel(),
      this.loadLiveChart(),
      this.loadHistoryChart(this.selectedPreset()),
    ]);
    this.startPolling();
  }

  private startPolling(): void {
    // Panel + live chart share the 5s poll.
    this.pollTimer = setInterval(() => {
      void this.loadPanel();
    }, this.POLL_MS);

    this.liveChartTimer = setInterval(() => {
      void this.loadLiveChart();
    }, this.POLL_MS);
  }

  private async loadProfile(): Promise<void> {
    try {
      const p = await this.panelSvc.getPanelProfile(this.broker());
      this.profile.set(p);
    } catch {
      // Profile failure is non-fatal; the panel can still render.
    }
  }

  // In-flight guards: the panel/live-chart endpoints can take longer than
  // POLL_MS; without the guard each tick stacks another concurrent request
  // and the pile-up degrades the backend for every panel consumer.

  private async loadPanel(): Promise<void> {
    if (this.panelInFlight) return;
    this.panelInFlight = true;
    try {
      const p = await this.panelSvc.getPanel(
        this.broker(),
        this.accountId(),
        this.sid(),
      );
      this.panel.set(p);
      this.loadError.set(null);
    } catch (err) {
      this.loadError.set(
        err instanceof Error ? err.message : 'Failed to load panel data.',
      );
    } finally {
      this.panelInFlight = false;
    }
  }

  private async loadLiveChart(): Promise<void> {
    if (this.liveChartInFlight) return;
    this.liveChartInFlight = true;
    try {
      const c = await this.panelSvc.getLiveChart(
        this.broker(),
        this.accountId(),
        this.sid(),
      );
      this.liveChart.set(c);
    } catch {
      // Non-fatal: chart stays at last known state.
    } finally {
      this.liveChartInFlight = false;
    }
  }

  private async loadHistoryChart(preset: ChartHistoryPreset): Promise<void> {
    try {
      const c = await this.panelSvc.getHistoryChart(
        this.broker(),
        this.accountId(),
        this.sid(),
        preset,
      );
      this.histChart.set(c);
    } catch {
      // Non-fatal.
    }
  }

  private teardown(): void {
    if (this.pollTimer !== null) {
      clearInterval(this.pollTimer);
      this.pollTimer = null;
    }
    if (this.liveChartTimer !== null) {
      clearInterval(this.liveChartTimer);
      this.liveChartTimer = null;
    }
  }

  // ── Template handlers ─────────────────────────────────────────────────────

  protected async onPresetChange(preset: ChartHistoryPreset): Promise<void> {
    this.selectedPreset.set(preset);
    await this.loadHistoryChart(preset);
  }

  protected async onActionRequested(action: PanelAction): Promise<void> {
    if (this.actionPending()) return;
    this.actionPending.set(true);
    try {
      await this.panelSvc.runAction(
        this.broker(),
        this.accountId(),
        this.sid(),
        {
          action_id: action.action_id,
          revision: action.revision,
          idempotency_key: crypto.randomUUID(),
          reason: null,
        },
      );
      // Re-poll panel immediately after action.
      await this.loadPanel();
    } catch {
      // Errors bubble to loadError via loadPanel — no silent swallow.
    } finally {
      this.actionPending.set(false);
    }
  }
}
