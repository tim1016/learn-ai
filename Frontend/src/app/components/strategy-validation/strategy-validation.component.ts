import {
  ChangeDetectionStrategy,
  Component,
  computed,
  inject,
  resource,
  signal,
} from '@angular/core';
import { RouterLink } from '@angular/router';

import { StrategyValidationService } from '../../services/strategy-validation.service';
import type {
  StrategyValidationDetail,
  StrategyValidationFlag,
  StrategyValidationSummary,
} from '../../services/strategy-validation.types';
import { ReceiptLabelPipe } from '../../shared/pipes/receipt-label.pipe';

@Component({
  selector: 'app-strategy-validation',
  imports: [RouterLink, ReceiptLabelPipe],
  templateUrl: './strategy-validation.component.html',
  styleUrl: './strategy-validation.component.scss',
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class StrategyValidationComponent {
  private readonly service = inject(StrategyValidationService);
  private readonly selectedOverride = signal<string | null>(null);
  protected readonly flagChoice = signal<StrategyValidationFlag>('validated');
  protected readonly flagReason = signal<string>('');
  protected readonly backtestId = signal<string>('');
  protected readonly actionBusy = signal<boolean>(false);
  protected readonly actionError = signal<string | null>(null);
  protected readonly actionMessage = signal<string | null>(null);

  protected readonly catalog = resource({
    loader: () => this.service.getCatalog(),
  });

  protected readonly strategies = computed(() => this.catalog.value()?.strategies ?? []);
  protected readonly validatedCount = computed(
    () => this.strategies().filter((strategy) => strategy.deployable).length,
  );
  protected readonly needsValidationCount = computed(
    () => this.strategies().filter((strategy) => !this.isAcceptedForDeploy(strategy)).length,
  );
  protected readonly selectedKey = computed(
    () =>
      this.selectedOverride() ??
      this.strategies().find((strategy) => this.isAcceptedForDeploy(strategy))?.strategy_key ??
      this.strategies()[0]?.strategy_key ??
      null,
  );

  protected readonly detail = resource<StrategyValidationDetail | null, string | null>({
    params: () => this.selectedKey(),
    loader: ({ params }) => {
      if (params === null) return Promise.resolve(null);
      return this.service.getDetail(params);
    },
  });

  protected selectStrategy(strategy: StrategyValidationSummary): void {
    this.selectedOverride.set(strategy.strategy_key);
    this.flagChoice.set(strategy.validation_state === 'validated' ? 'invalidated' : 'validated');
    this.flagReason.set('');
    this.backtestId.set(strategy.qc_cloud_backtest_id ?? '');
    this.actionError.set(null);
    this.actionMessage.set(null);
  }

  protected stateLabel(strategy: StrategyValidationSummary | StrategyValidationDetail): string {
    return strategy.validation_state === 'validated' ? 'Validated' : 'Needs validation';
  }

  protected isAcceptedForDeploy(
    strategy: StrategyValidationSummary | StrategyValidationDetail,
  ): boolean {
    return (
      strategy.validation_state === 'validated' &&
      strategy.deployable &&
      strategy.behavioral_equivalence?.verdict === 'accepted_for_deploy'
    );
  }

  protected deployQueryParams(strategy: StrategyValidationDetail): Record<string, string> {
    return {
      strategy_key: strategy.strategy_key,
      spec_path: strategy.settings_file_ref ?? '',
      signal_stream: strategy.validation_case_symbol ?? '',
      qc_backtest_id: strategy.qc_cloud_backtest_id ?? '',
      qc_audit_copy_path: strategy.audit_copy_ref ?? '',
    };
  }

  protected engineLabQueryParams(strategy: StrategyValidationDetail): Record<string, string> {
    return {
      strategy: strategy.strategy_key,
      engine: this.engineLabEngine(strategy),
      symbol: strategy.validation_case_symbol ?? 'SPY',
      tab: 'configuration',
      qc_backtest_id: strategy.qc_cloud_backtest_id ?? '',
    };
  }

  private engineLabEngine(strategy: StrategyValidationDetail): string {
    return strategy.validator_code_ref ? 'both' : 'python';
  }

  protected isSelected(strategy: StrategyValidationSummary): boolean {
    return strategy.strategy_key === this.selectedKey();
  }

  protected divergenceEntries(detail: StrategyValidationDetail): [string, number][] {
    return Object.entries(detail.diagnostics?.divergence_counts ?? {});
  }

  protected setFlagChoice(event: Event): void {
    if (!(event.target instanceof HTMLInputElement)) return;
    const value = event.target.value;
    if (value === 'validated' || value === 'invalidated') {
      this.flagChoice.set(value);
    }
  }

  protected setFlagReason(event: Event): void {
    if (event.target instanceof HTMLTextAreaElement) {
      this.flagReason.set(event.target.value);
    }
  }

  protected setBacktestId(event: Event): void {
    if (event.target instanceof HTMLInputElement) {
      this.backtestId.set(event.target.value);
    }
  }

  protected flagLabel(flag: StrategyValidationFlag): string {
    return flag === 'validated' ? 'Accepted' : 'Rejected';
  }

  protected formatMs(ms: number | null | undefined): string {
    if (ms === null || ms === undefined) return 'n/a';
    return new Date(ms).toLocaleString();
  }

  protected async refreshValidationEvidence(): Promise<void> {
    const key = this.selectedKey();
    if (key === null || this.actionBusy()) return;
    this.actionBusy.set(true);
    this.actionError.set(null);
    this.actionMessage.set(null);
    try {
      const result = await this.service.refreshValidationEvidence(key);
      this.actionMessage.set(`Validation evidence refreshed from ${result.refresh_id}.`);
      this.catalog.reload();
      this.detail.reload();
    } catch {
      this.actionError.set('Validation evidence could not be refreshed.');
    } finally {
      this.actionBusy.set(false);
    }
  }

  protected async submitFlag(): Promise<void> {
    const key = this.selectedKey();
    const reason = this.flagReason().trim();
    if (key === null || this.actionBusy()) return;
    if (reason === '') {
      this.actionError.set('A validation reason is required.');
      return;
    }
    const backtestId = this.backtestId().trim() || this.detail.value()?.qc_cloud_backtest_id?.trim() || '';
    if (this.flagChoice() === 'validated' && backtestId === '') {
      this.actionError.set('A QC Cloud backtest ID is required to accept validation evidence.');
      return;
    }
    this.actionBusy.set(true);
    this.actionError.set(null);
    this.actionMessage.set(null);
    try {
      const detail = await this.service.flagValidation(key, {
        flag: this.flagChoice(),
        reason,
        ...(this.flagChoice() === 'validated' ? { qc_cloud_backtest_id: backtestId } : {}),
      });
      this.actionMessage.set(`${this.flagLabel(detail.current_flag_event?.flag ?? this.flagChoice())} flag saved.`);
      this.flagReason.set('');
      this.catalog.reload();
      this.detail.reload();
    } catch {
      this.actionError.set('Validation flag could not be saved.');
    } finally {
      this.actionBusy.set(false);
    }
  }
}
