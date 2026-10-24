import { ChangeDetectionStrategy, Component, computed, inject, input, output, resource } from '@angular/core';
import { RouterLink } from '@angular/router';

import { StrategyValidationService } from '../../services/strategy-validation.service';
import type { StrategyValidationDetail, StrategyValidationSummary } from '../../services/strategy-validation.types';
import { ReceiptLabelPipe } from '../../shared/pipes/receipt-label.pipe';
import { WorkspaceStateBlockComponent } from '../../shared/workspace/workspace-state-block.component';
import { exploreTabRoute } from '../explore/explore-tabs';
import { goldenTabRoute } from '../golden/golden-tabs';
import { readStateOf } from '../read-state';
import { PROOF_TONES, proofSummaryOf } from '../signal-strategy-evidence';
import { SignalStrategyChipComponent } from '../signal-strategy-chip.component';
import { ProofActionsComponent } from './proof-actions.component';
import { ProofEvidenceComponent } from './proof-evidence.component';
import { StrategyProofPipelineComponent } from './strategy-proof-pipeline/strategy-proof-pipeline.component';

/**
 * One strategy's program proof in full: the ordered proof dossier, whether it is
 * accepted for Deploy, the reconciliation facts, the strategy-level human flag and
 * the evidence behind them. It is about the program at its validated corpus point;
 * nothing here approves a symbol's settings, and it says so and leads to Golden,
 * where a selected baseline run is reviewed and a configuration is approved. A
 * proof that is current, stale, missing, blocked, rejected or unreadable says so
 * in those words, and a failed read is unknown, never missing.
 */
@Component({
  selector: 'app-proof-detail',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [
    ProofActionsComponent,
    ProofEvidenceComponent,
    ReceiptLabelPipe,
    RouterLink,
    SignalStrategyChipComponent,
    StrategyProofPipelineComponent,
    WorkspaceStateBlockComponent,
  ],
  templateUrl: './proof-detail.component.html',
  styleUrl: './proof-detail.component.scss',
})
export class ProofDetailComponent {
  private readonly service = inject(StrategyValidationService);

  /** The catalog's record for the strategy: its key says whose detail to read. */
  readonly summary = input.required<StrategyValidationSummary>();
  /** Something the proof says has changed, so the catalog's record should be read again. */
  readonly changed = output();

  private readonly read = resource({
    params: () => this.summary().strategy_key,
    loader: ({ params }) => this.service.getDetail(params),
  });

  protected readonly state = computed(() => readStateOf(this.read));
  protected readonly tones = PROOF_TONES;
  protected readonly summaryOf = proofSummaryOf;

  protected isHarness(detail: StrategyValidationDetail): boolean {
    return detail.strategy_category === 'operational_validation_harness';
  }

  protected isAccepted(detail: StrategyValidationDetail): boolean {
    return detail.validation_state === 'validated' && detail.deployable && detail.behavioral_equivalence?.verdict === 'accepted_for_deploy';
  }

  /** The strategy's Backtest, where it is diagnosed: its key is the address. */
  protected backtestRoute(detail: StrategyValidationDetail): readonly string[] {
    return exploreTabRoute(detail.strategy_key, 'backtest');
  }

  protected backtestQueryParams(detail: StrategyValidationDetail): Record<string, string> {
    return {
      engine: detail.validator_code_ref ? 'both' : 'python',
      symbol: detail.validation_case_symbol ?? 'SPY',
      tab: 'configuration',
    };
  }

  protected deployQueryParams(detail: StrategyValidationDetail): Record<string, string> {
    return { deploy: '', strategy: detail.strategy_key };
  }

  protected configurationRoute(detail: StrategyValidationDetail): readonly string[] {
    return goldenTabRoute(detail.strategy_key, 'configuration');
  }

  protected onChanged(): void {
    this.read.reload();
    this.changed.emit();
  }

  protected reload(): void {
    this.read.reload();
  }
}
