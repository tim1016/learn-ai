import { ChangeDetectionStrategy, Component, signal } from '@angular/core';
import { DialogModule } from 'primeng/dialog';
import { TooltipModule } from 'primeng/tooltip';

/**
 * Explains what LEAN validation covers — and what it deliberately does not.
 *
 * Every figure the platform shows is produced by the Python engine; the
 * vendored LEAN reference validates it at the level of summary statistics,
 * the closed-trade ledger and global run characteristics, never bar-by-bar.
 * Mounted next to engine figures on the surfaces where a reader could
 * otherwise compare them against a LEAN dashboard bar-for-bar and read an
 * expected granularity difference as a defect (Strategy Lab LEAN statistics,
 * Grid Search cells, Walk-Forward verdicts).
 *
 * Frontend-authored explainer copy, like the in-sample / out-of-sample notes
 * it sits beside — no backend identifier or backend-authored prose passes
 * through it.
 */
@Component({
  selector: 'app-validation-scope-note',
  imports: [DialogModule, TooltipModule],
  templateUrl: './validation-scope-note.component.html',
  styleUrl: './validation-scope-note.component.scss',
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class ValidationScopeNoteComponent {
  protected readonly visible = signal(false);

  protected readonly tooltip =
    'LEAN validation covers summary statistics, trades and run totals — not every bar. Click for what that means for these figures.';

  open(): void {
    this.visible.set(true);
  }

  onVisibleChange(visible: boolean): void {
    this.visible.set(visible);
  }
}
