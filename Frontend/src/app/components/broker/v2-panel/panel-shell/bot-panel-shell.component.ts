import {
  ChangeDetectionStrategy,
  Component,
  DestroyRef,
  computed,
  inject,
  input,
  linkedSignal,
  resource,
  signal,
} from '@angular/core';
import { HttpErrorResponse } from '@angular/common/http';
import { toSignal } from '@angular/core/rxjs-interop';
import { ActivatedRoute, Router } from '@angular/router';

import type {
  ChartHistoryPreset,
  ChartLiveResolution,
  PanelAction,
  PanelActionResult,
} from '../lib/broker-v2-panel.types';
import { BrokerV2PanelService } from '../lib/broker-v2-panel.service';
import { TraderLensComponent } from '../trader-lens/trader-lens.component';
import { OperatorLensComponent } from '../operator-lens/operator-lens.component';
import { PanelHeaderComponent } from './panel-header.component';
import {
  type ActionReceiptView,
  PanelActionReceiptComponent,
} from './panel-action-receipt.component';

type PanelLens = 'trader' | 'operator';

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
  imports: [
    PanelHeaderComponent,
    PanelActionReceiptComponent,
    TraderLensComponent,
    OperatorLensComponent,
  ],
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
  private readonly route = inject(ActivatedRoute);
  private readonly router = inject(Router);

  // ── Active lens ──────────────────────────────────────────────────────────
  // Reads the `?lens=` query param if provided; defaults to 'trader'.
  // Set via selectLens() or the tab toggle in the template.

  private readonly queryParams = toSignal(this.route.queryParamMap, {
    initialValue: this.route.snapshot.queryParamMap,
  });
  protected readonly activeLens = linkedSignal<PanelLens>(() =>
    this.queryParams().get('lens') === 'operator' ? 'operator' : 'trader',
  );

  // ── Internal state ────────────────────────────────────────────────────────

  protected readonly selectedPreset = signal<ChartHistoryPreset>('1D');
  protected readonly liveResolution = signal<ChartLiveResolution>('5s');
  protected readonly selectedTransactionRef = signal<string | null>(null);
  protected readonly actionPending = signal(false);
  protected readonly actionReceipt = signal<ActionReceiptView | null>(null);

  protected readonly panel = resource({
    params: () => ({
      ...this.routeParams(),
      transactionRef:
        this.activeLens() === 'operator'
          ? (this.selectedTransactionRef() ?? undefined)
          : undefined,
    }),
    loader: ({ params }) =>
      this.panelSvc.getPanel(
        params.broker,
        params.accountId,
        params.sid,
        params.transactionRef,
      ),
  });

  protected readonly profile = resource({
    params: () => this.broker(),
    loader: ({ params }) => this.panelSvc.getPanelProfile(params),
  });

  protected readonly liveChart = resource({
    params: () =>
      this.activeLens() === 'trader'
        ? { ...this.routeParams(), resolution: this.liveResolution() }
        : undefined,
    loader: ({ params }) =>
      this.panelSvc.getLiveChart(
        params.broker,
        params.accountId,
        params.sid,
        params.resolution,
      ),
  });

  protected readonly histChart = resource({
    params: () =>
      this.activeLens() === 'trader'
        ? { ...this.routeParams(), preset: this.selectedPreset() }
        : undefined,
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
    const error = this.panel.error() ?? this.profile.error();
    if (error === undefined) return null;
    return error instanceof Error ? error.message : 'Failed to load panel data.';
  });

  // ── Lifecycle ─────────────────────────────────────────────────────────────

  constructor() {
    const pollTimer = setInterval(() => {
      if (!this.panel.isLoading()) {
        this.panel.reload();
      }
      if (this.activeLens() === 'trader' && !this.liveChart.isLoading()) {
        this.liveChart.reload();
      }
    }, 5_000);
    this.destroyRef.onDestroy(() => clearInterval(pollTimer));
  }

  // ── Shell helpers for S4 extension ───────────────────────────────────────

  /** Called by the tab bar to switch between lenses. */
  protected selectLens(lens: PanelLens): void {
    this.activeLens.set(lens);
    if (lens === 'trader') {
      this.selectedTransactionRef.set(null);
    }
    void this.router.navigate([], {
      relativeTo: this.route,
      queryParams: { lens },
      queryParamsHandling: 'merge',
      replaceUrl: true,
    });
  }

  protected onLensKeydown(event: KeyboardEvent): void {
    const nextLens =
      event.key === 'ArrowRight' || event.key === 'End'
        ? 'operator'
        : event.key === 'ArrowLeft' || event.key === 'Home'
          ? 'trader'
          : null;
    if (nextLens === null) return;
    event.preventDefault();
    this.selectLens(nextLens);
    const target = (event.currentTarget as HTMLElement | null)?.parentElement?.querySelector(
      `[data-lens="${nextLens}"]`,
    );
    if (target instanceof HTMLElement) target.focus();
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

  protected onLiveResolutionChange(resolution: ChartLiveResolution): void {
    this.liveResolution.set(resolution);
  }

  protected onTransactionSelected(transactionRef: string): void {
    this.selectedTransactionRef.set(transactionRef);
  }

  protected dismissActionReceipt(): void {
    this.actionReceipt.set(null);
  }

  protected async onActionRequested(action: PanelAction): Promise<void> {
    if (this.actionPending()) return;
    this.actionPending.set(true);
    this.actionReceipt.set(null);
    try {
      const result = await this.panelSvc.runBotAction(
        this.broker(),
        this.accountId(),
        this.sid(),
        action,
      );
      this.actionReceipt.set(this.successReceipt(result));
      this.panel.reload();
    } catch (error) {
      const receipt = this.errorReceipt(error, action);
      this.actionReceipt.set(receipt);
    } finally {
      this.actionPending.set(false);
    }
  }

  private successReceipt(result: PanelActionResult): ActionReceiptView {
    return {
      actionId: result.action_id,
      outcome: 'success',
      receiptId: result.receipt_id,
      recordedAtMs: result.recorded_at_ms,
      message: result.message,
      remediation: null,
    };
  }

  private errorReceipt(error: unknown, action: PanelAction): ActionReceiptView {
    const detail =
      error instanceof HttpErrorResponse &&
      typeof error.error === 'object' &&
      error.error !== null &&
      'detail' in error.error &&
      typeof error.error.detail === 'object' &&
      error.error.detail !== null
        ? (error.error.detail as Record<string, unknown>)
        : null;
    const outcome = detail?.['outcome'];
    return {
      actionId:
        typeof detail?.['action_id'] === 'string'
          ? detail['action_id']
          : action.action_id,
      outcome:
        outcome === 'conflict' || outcome === 'failure' || outcome === 'unknown'
          ? outcome
          : 'unknown',
      receiptId:
        typeof detail?.['receipt_id'] === 'string' ? detail['receipt_id'] : null,
      recordedAtMs:
        typeof detail?.['recorded_at_ms'] === 'number'
          ? detail['recorded_at_ms']
          : Date.now(),
      message:
        typeof detail?.['message'] === 'string'
          ? detail['message']
          : error instanceof Error
            ? error.message
            : `Action "${action.label}" failed.`,
      remediation:
        typeof detail?.['why'] === 'string' ? detail['why'] : null,
    };
  }
}
