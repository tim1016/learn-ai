import { ChangeDetectionStrategy, Component, input, output } from '@angular/core';
import { ButtonModule } from 'primeng/button';

import { ConfirmDeleteComponent } from './confirm-delete.component';

/**
 * Cancel / Finish / Delete for a fenced research record (a grid search or a
 * walk-forward study): Cancel while it runs, Finish when it is resumable and
 * the refusal reason otherwise, Delete behind a confirmation.
 */
@Component({
  selector: 'app-record-controls',
  imports: [ButtonModule, ConfirmDeleteComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    @if (running()) {
      <button pButton type="button" size="small" severity="secondary" [outlined]="true" [disabled]="busy()" (click)="cancelRequested.emit()"><i class="pi pi-stop" aria-hidden="true"></i><span>Cancel</span></button>
    }
    @if (resumable()) {
      <button pButton type="button" size="small" [disabled]="busy()" (click)="finishRequested.emit()"><i class="pi pi-forward" aria-hidden="true"></i><span>Finish</span></button>
    } @else if (resumeRefusal() && incomplete()) {
      <span class="muted" [title]="resumeRefusal() ?? ''">Finish unavailable — {{ resumeRefusal() }}</span>
    }
    <app-confirm-delete [outlined]="true" [busy]="busy()" [prompt]="running() ? cancelAndDeletePrompt() : deletePrompt()" (confirmed)="deleteConfirmed.emit()" />
  `,
  styles: `:host { display: contents; } .muted { color: var(--text-secondary); font-size: var(--fs-xs); }`,
})
export class RecordControlsComponent {
  readonly running = input.required<boolean>();
  readonly resumable = input.required<boolean>();
  readonly resumeRefusal = input<string | null>(null);
  readonly incomplete = input(false);
  readonly busy = input(false);
  readonly deletePrompt = input.required<string>();
  readonly cancelAndDeletePrompt = input.required<string>();
  readonly cancelRequested = output();
  readonly finishRequested = output();
  readonly deleteConfirmed = output();
}
