import { ChangeDetectionStrategy, Component, computed, input } from '@angular/core';
import type { BarsSpec, DataPolicy } from '../../../models/data-policy';

function barsLabel(bars: BarsSpec): string {
  const unit = bars.timespan === 'day' ? 'day' : bars.timespan;
  return `${bars.multiplier}-${unit}`;
}

/**
 * Explains the three validation gates a run must clear before deploy.
 * This is a neutral, informational ladder — it does NOT assert per-run
 * readiness. Parity verdicts land on the run report; the external receipt
 * is attached in Strategy Validation. Rendering a satisfied ✓ here would
 * be an unearned claim, so the steps carry no ready/blocked state.
 */
@Component({
  selector: 'app-validation-evidence-card',
  templateUrl: './validation-evidence-card.component.html',
  styleUrl: './validation-evidence-card.component.scss',
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class ValidationEvidenceCardComponent {
  readonly policy = input.required<DataPolicy>();

  readonly inputBarsLabel = computed(() => barsLabel(this.policy().input_bars));
  readonly strategyBarsLabel = computed(() => barsLabel(this.policy().strategy_bars));
  readonly consolidatesInputBars = computed(() =>
    this.inputBarsLabel() !== this.strategyBarsLabel(),
  );
}
