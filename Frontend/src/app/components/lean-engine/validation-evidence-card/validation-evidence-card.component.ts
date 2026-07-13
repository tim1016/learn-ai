import { ChangeDetectionStrategy, Component, input } from '@angular/core';

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
  readonly symbol = input.required<string>();
  readonly resolution = input.required<string>();
}
