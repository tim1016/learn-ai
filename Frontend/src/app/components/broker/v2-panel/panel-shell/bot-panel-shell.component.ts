import {
  ChangeDetectionStrategy,
  Component,
  DestroyRef,
  computed,
  effect,
  inject,
  input,
  linkedSignal,
  resource,
  signal,
} from '@angular/core';
import { toSignal } from '@angular/core/rxjs-interop';
import { ActivatedRoute, Router } from '@angular/router';
import { MessageService } from 'primeng/api';
import { firstValueFrom } from 'rxjs';

import type {
  HistoricalExecutionRecoveryPlan,
  SqliteSafeFlattenPlan,
} from '../../../../api/alpaca.types';
import { SafeFlattenPlanComponent } from '../../shared/safe-flatten-plan/safe-flatten-plan.component';
import { TypedHaltConfirmComponent } from '../../shared/typed-halt-confirm/typed-halt-confirm.component';
import type {
  ChartHistoryPreset,
  ChartLiveResolution,
  PanelAction,
  PanelActionResult,
  PanelActionTrigger,
} from '../lib/broker-v2-panel.types';
import { BrokerV2PanelService } from '../lib/broker-v2-panel.service';
import { BotPanelLiveStore } from '../lib/bot-panel-live-store.service';
import { BrokersService } from '../../../../services/brokers.service';
import { MarketDataService } from '../../../../services/market-data.service';
import type { TickerQuoteView } from '../../../../shared/ticker-quote/ticker-quote.component';
import {
  actionOutcomeToast,
  deriveActionRejection,
  extractActionErrorDetail,
} from '../lib/panel-action-outcome';
import { TraderLensComponent } from '../trader-lens/trader-lens.component';
import { OperatorLensComponent } from '../operator-lens/operator-lens.component';
import {
  type ActionReceiptView,
  PanelActionReceiptComponent,
} from './panel-action-receipt.component';

type PanelLens = 'trader' | 'operator';

interface HistoricalExecutionRecoveryDraft {
  readonly action: PanelAction;
  readonly plan: HistoricalExecutionRecoveryPlan;
}

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
    PanelActionReceiptComponent,
    SafeFlattenPlanComponent,
    TypedHaltConfirmComponent,
    TraderLensComponent,
    OperatorLensComponent,
  ],
  templateUrl: './bot-panel-shell.component.html',
  styleUrl: './bot-panel-shell.component.scss',
  providers: [BotPanelLiveStore],
  host: {
    '[class.bot-panel-shell--trader]': "activeLens() === 'trader'",
  },
})
export class BotPanelShellComponent {
  // ── Route inputs (Angular route input binding) ────────────────────────────

  readonly broker = input.required<string>();
  readonly accountId = input.required<string>();
  readonly sid = input.required<string>();

  // ── Services ──────────────────────────────────────────────────────────────

  private readonly panelSvc = inject(BrokerV2PanelService);
  private readonly brokers = inject(BrokersService);
  private readonly marketData = inject(MarketDataService);
  private readonly liveStore = inject(BotPanelLiveStore);
  private readonly destroyRef = inject(DestroyRef);
  private readonly route = inject(ActivatedRoute);
  private readonly router = inject(Router);
  private readonly messageService = inject(MessageService);

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
  private readonly routeIdentity = computed(() => this.routeParams());
  protected readonly reductionPlan = linkedSignal({
    source: this.routeIdentity,
    computation: (): SqliteSafeFlattenPlan | null => null,
  });
  protected readonly historicalRecoveryDraft = linkedSignal({
    source: this.routeIdentity,
    computation: (): HistoricalExecutionRecoveryDraft | null => null,
  });

  protected readonly panel = computed(() => this.liveStore.snapshot()?.panel ?? null);
  protected readonly liveChart = computed(() => {
    const chart = this.liveStore.snapshot()?.live_chart ?? null;
    return chart?.resolution === this.liveResolution() ? chart : null;
  });
  protected readonly liveStreamStatus = this.liveStore.status;

  protected readonly profile = resource({
    params: () => this.broker(),
    loader: ({ params }) => this.panelSvc.getPanelProfile(params),
  });

  private readonly marketSnapshot = resource({
    params: () => this.panel()?.symbol,
    loader: ({ params: symbol }) =>
      firstValueFrom(this.marketData.getStockSnapshot(symbol)),
  });

  protected readonly tickerQuote = computed<TickerQuoteView | null>(() => {
    const snapshot = this.marketSnapshot.hasValue()
      ? this.marketSnapshot.value().snapshot
      : null;
    const price = snapshot?.day?.close ?? snapshot?.min?.close;
    if (price === null || price === undefined) return null;
    return {
      ticker: snapshot?.ticker ?? this.panel()?.symbol ?? '',
      price,
      change: snapshot?.todaysChange,
      changePercent: snapshot?.todaysChangePercent ?? null,
    };
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
    () => this.panel() !== null && this.profile.hasValue(),
  );

  protected readonly loadError = computed(() => {
    const liveError = this.liveStore.error();
    if (liveError !== null) return liveError;
    const error = this.profile.error();
    if (error === undefined || error === null) return null;
    return error instanceof Error ? error.message : 'Failed to load panel data.';
  });

  // ── Lifecycle ─────────────────────────────────────────────────────────────

  constructor() {
    effect(() => {
      void this.liveStore.start({
        ...this.routeParams(),
        resolution: this.liveResolution(),
      });
    });
    this.destroyRef.onDestroy(() => {
      this.liveStore.stop();
    });
  }

  // ── Shell helpers for S4 extension ───────────────────────────────────────

  /** Called by the tab bar to switch between lenses. */
  protected selectLens(lens: PanelLens): void {
    this.activeLens.set(lens);
    if (lens === 'trader') {
      this.selectedTransactionRef.set(null);
      this.liveStore.clearSelectedTransaction();
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
    void this.liveStore.selectTransaction(transactionRef);
  }

  protected dismissActionReceipt(): void {
    this.actionReceipt.set(null);
  }

  protected async onActionRequested({ action, reason }: PanelActionTrigger): Promise<void> {
    if (this.actionPending()) return;
    if (action.action_id === 'open_custody_timeline') {
      this.selectLens('operator');
      return;
    }
    if (action.action_id === 'prepare_safe_flatten') {
      await this.prepareSafeFlatten(action);
      return;
    }
    if (action.action_id === 'recover_exact_execution_evidence') {
      await this.prepareHistoricalExecutionRecovery(action);
      return;
    }
    this.actionPending.set(true);
    this.actionReceipt.set(null);
    try {
      // runBotAction is resilient: a Stop-409 (transient token flip) is retried
      // once with a fresh token instead of dead-ending the operator (defect #10).
      const result = await this.panelSvc.runBotAction(
        this.broker(),
        this.accountId(),
        this.sid(),
        action,
        reason,
      );
      const receipt = this.successReceipt(result);
      this.actionReceipt.set(receipt);
      this.messageService.add(actionOutcomeToast('success', receipt.message));
      await this.liveStore.refresh();
    } catch (error) {
      const receipt = this.errorReceipt(error, action);
      this.actionReceipt.set(receipt);
      this.messageService.add(actionOutcomeToast(receipt.outcome, receipt.message, receipt.remediation));
      // The rejection is always pre-execution (see runBotAction's doc), so the
      // operator's last-seen panel state is now stale relative to whatever
      // changed underneath it — refresh so "Ready to resume" doesn't linger
      // after a resume was just refused for no longer being ready.
      await this.liveStore.refresh();
    } finally {
      this.actionPending.set(false);
    }
  }

  private async prepareSafeFlatten(action: PanelAction): Promise<void> {
    this.selectLens('operator');
    const requestIdentity = this.routeIdentity();
    this.actionPending.set(true);
    this.actionReceipt.set(null);
    this.reductionPlan.set(null);
    try {
      const capability = await this.brokers.checkSqliteRecoveryAction(
        this.accountId(),
        { action_id: 'prepare_safe_flatten', concurrency_token: action.concurrency_token },
        this.sid(),
      );
      if (requestIdentity !== this.routeIdentity()) return;
      this.reductionPlan.set(capability.reduction_plan);
      this.messageService.add({
        severity: 'info',
        summary: capability.label,
        detail: capability.next_step,
      });
    } catch (error) {
      if (requestIdentity !== this.routeIdentity()) return;
      const receipt = this.errorReceipt(error, action);
      this.actionReceipt.set(receipt);
      this.messageService.add(
        actionOutcomeToast(receipt.outcome, receipt.message, receipt.remediation),
      );
      await this.liveStore.refresh();
    } finally {
      this.actionPending.set(false);
    }
  }

  protected cancelHistoricalExecutionRecovery(): void {
    this.historicalRecoveryDraft.set(null);
  }

  protected historicalRecoveryMessage(plan: HistoricalExecutionRecoveryPlan): string {
    return `Alpaca paper activity ${plan.execution_id} records ${plan.exact_side} ${plan.exact_quantity} ${plan.exact_symbol} at ${plan.exact_price}. It exactly matches cumulative recovery fill ${plan.cumulative_fill_id}.`;
  }

  protected async confirmHistoricalExecutionRecovery(): Promise<void> {
    const draft = this.historicalRecoveryDraft();
    if (draft === null || this.actionPending()) return;
    const requestIdentity = this.routeIdentity();
    this.actionPending.set(true);
    this.actionReceipt.set(null);
    try {
      const receipt = await this.panelSvc.confirmHistoricalExecutionRecovery(
        this.accountId(),
        this.sid(),
        draft.plan,
      );
      if (requestIdentity !== this.routeIdentity()) return;
      this.historicalRecoveryDraft.set(null);
      const message = receipt.applied
        ? `${draft.action.label} completed. The Clerk recorded exact evidence without changing economic totals.`
        : `${draft.action.label} had already completed; the durable result was replayed.`;
      const actionReceipt: ActionReceiptView = {
        actionId: draft.action.action_id,
        outcome: 'success',
        receiptId: receipt.receipt_id,
        recordedAtMs: receipt.recorded_at_ms,
        message,
        remediation: null,
      };
      this.actionReceipt.set(actionReceipt);
      this.messageService.add(actionOutcomeToast('success', actionReceipt.message));
      await this.liveStore.refresh();
    } catch (error) {
      if (requestIdentity !== this.routeIdentity()) return;
      this.historicalRecoveryDraft.set(null);
      const receipt = this.errorReceipt(error, draft.action);
      this.actionReceipt.set(receipt);
      this.messageService.add(actionOutcomeToast(receipt.outcome, receipt.message, receipt.remediation));
      await this.liveStore.refresh();
    } finally {
      this.actionPending.set(false);
    }
  }

  private async prepareHistoricalExecutionRecovery(action: PanelAction): Promise<void> {
    const requestIdentity = this.routeIdentity();
    this.actionPending.set(true);
    this.actionReceipt.set(null);
    this.historicalRecoveryDraft.set(null);
    try {
      const plan = await this.panelSvc.prepareHistoricalExecutionRecovery(
        this.accountId(),
        this.sid(),
        action.concurrency_token,
      );
      if (requestIdentity !== this.routeIdentity()) return;
      this.historicalRecoveryDraft.set({ action, plan });
    } catch (error) {
      if (requestIdentity !== this.routeIdentity()) return;
      const receipt = this.errorReceipt(error, action);
      this.actionReceipt.set(receipt);
      this.messageService.add(actionOutcomeToast(receipt.outcome, receipt.message, receipt.remediation));
      await this.liveStore.refresh();
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
    const detail = extractActionErrorDetail(error);
    const rejection = deriveActionRejection(error, `Action "${action.label}" failed.`);
    return {
      actionId:
        typeof detail?.['action_id'] === 'string'
          ? detail['action_id']
          : action.action_id,
      outcome: rejection.outcome,
      receiptId:
        typeof detail?.['receipt_id'] === 'string' ? detail['receipt_id'] : null,
      recordedAtMs:
        typeof detail?.['recorded_at_ms'] === 'number'
          ? detail['recorded_at_ms']
          : Date.now(),
      message: rejection.message,
      remediation: rejection.why,
    };
  }
}
