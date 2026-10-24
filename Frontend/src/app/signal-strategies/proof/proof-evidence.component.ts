import { ChangeDetectionStrategy, Component, computed, input } from '@angular/core';

import type { StrategyValidationDetail } from '../../services/strategy-validation.types';
import { ReceiptLabelPipe } from '../../shared/pipes/receipt-label.pipe';
import { LeanTwinSourceComponent } from './lean-twin-source/lean-twin-source.component';
import { QuantConnectReferenceCodeComponent } from './quantconnect-reference-code/quantconnect-reference-code.component';

/**
 * The reconciliation evidence behind a strategy's proof: how many trades matched
 * and validated against the reference, the provenance of the files compared, the
 * divergence categories, the QuantConnect reference code (a production candidate's
 * only) and the registered LEAN twin source. Read-only. It describes the
 * program's corpus case, not any symbol's settings.
 */
@Component({
  selector: 'app-proof-evidence',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [LeanTwinSourceComponent, QuantConnectReferenceCodeComponent, ReceiptLabelPipe],
  templateUrl: './proof-evidence.component.html',
  styleUrl: './proof-evidence.component.scss',
})
export class ProofEvidenceComponent {
  readonly detail = input.required<StrategyValidationDetail>();

  protected readonly isHarness = computed(() => this.detail().strategy_category === 'operational_validation_harness');
  protected readonly divergences = computed(() => Object.entries(this.detail().diagnostics?.divergence_counts ?? {}));
}
