import { ChangeDetectionStrategy, Component, input, output, signal } from '@angular/core';
import { ButtonModule } from 'primeng/button';

/** A delete button that asks once before emitting `confirmed`; the confirm state is its own. */
@Component({
  selector: 'app-confirm-delete',
  imports: [ButtonModule],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    @if (!confirming()) {
      <button pButton type="button" size="small" severity="danger" [text]="!outlined()" [outlined]="outlined()" [disabled]="busy()" [attr.aria-label]="ariaLabel()" (click)="confirming.set(true)">{{ label() }}</button>
    } @else {
      <span class="confirm" role="group" aria-label="Confirm deletion">
        @if (prompt(); as text) { <span>{{ text }}</span> }
        <button pButton type="button" size="small" severity="danger" [disabled]="busy()" (click)="confirm()">Confirm delete</button>
        <button pButton type="button" size="small" severity="secondary" [text]="true" (click)="confirming.set(false)">Keep</button>
      </span>
    }
  `,
  styles: `:host { display: inline-flex; } .confirm { display: inline-flex; align-items: center; gap: var(--space-2); font-size: var(--fs-xs); }`,
})
export class ConfirmDeleteComponent {
  readonly label = input('Delete');
  readonly ariaLabel = input<string | null>(null);
  readonly prompt = input<string | null>(null);
  readonly busy = input(false);
  readonly outlined = input(false);
  readonly confirmed = output();

  readonly confirming = signal(false);

  confirm(): void {
    this.confirming.set(false);
    this.confirmed.emit();
  }
}
