import { ChangeDetectionStrategy, Component, input } from '@angular/core';

/**
 * Empty state shown before a run completes. The header reflects the real
 * current configuration; the body is an honest "nothing yet" message. It
 * deliberately shows no fabricated equity curve, trade markers, or
 * placeholder metrics — an invented preview would misrepresent results.
 */
@Component({
  selector: 'app-validation-stage-placeholder',
  templateUrl: './validation-stage-placeholder.component.html',
  styleUrl: './validation-stage-placeholder.component.scss',
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class ValidationStagePlaceholderComponent {
  readonly symbol = input.required<string>();
  readonly resolution = input.required<string>();
  readonly fillMode = input.required<string>();
  readonly engine = input.required<string>();
  /** Data-policy / bar-consolidation summary for the current configuration. */
  readonly dataPolicyNote = input('');
}
