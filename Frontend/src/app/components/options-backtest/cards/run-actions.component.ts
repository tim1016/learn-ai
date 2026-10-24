import { ChangeDetectionStrategy, Component, input, output } from '@angular/core';
import { ButtonModule } from 'primeng/button';

/**
 * Check data and Start backtest, with the shortcut Start answers to and, when
 * either waits, what it waits for. It decides nothing: the page that owns the
 * run says which can be pressed and why not.
 */
@Component({
  selector: 'app-options-backtest-run-actions',
  imports: [ButtonModule],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './run-actions.component.html',
  styleUrl: './run-actions.component.scss',
})
export class RunActionsComponent {
  /** Whether Check data can be asked: the setup is read back and both dates are picked. */
  readonly canCheck = input(false);
  /** Whether a backtest can be started: the same, and none is going. */
  readonly canStart = input(false);
  /** What Check data and Start wait for, or that a backtest is going. */
  readonly hint = input<string | null>(null);
  readonly check = output();
  readonly start = output();
}
