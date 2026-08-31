import { ChangeDetectionStrategy, Component, input, output } from '@angular/core';

import { ARCHIVE_CONFIRM_TOKEN } from './archive-confirm-token';

/**
 * The typed confirmation that commits a batch archive.
 *
 * The same word the backend requires on the single-bot action's typed
 * confirmation. Archiving N registrations is strictly more consequential than
 * archiving one, so the batch asks for the same deliberate act once rather
 * than waiving it because the surface is bulk.
 */
@Component({
  selector: 'app-cohort-archive-commit',
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <footer class="cohort-archive__commit">
      <label for="cohort-archive-confirm">
        Type {{ token }} to confirm archiving {{ selectedCount() }}
        {{ selectedCount() === 1 ? 'bot' : 'bots' }}
      </label>
      <input
        id="cohort-archive-confirm"
        type="text"
        autocomplete="off"
        [value]="confirmText()"
        [disabled]="!selectedCount()"
        (input)="confirmTextChange.emit($any($event.target).value)"
      />
      <button
        type="button"
        class="cohort-archive__submit"
        [disabled]="!canSubmit()"
        [attr.aria-busy]="submitting()"
        (click)="submitted.emit()"
      >
        {{ submitting() ? 'Archiving…' : 'Archive ' + selectedCount() }}
      </button>
    </footer>
  `,
  styleUrl: './cohort-archive-commit.component.scss',
})
export class CohortArchiveCommitComponent {
  readonly selectedCount = input.required<number>();
  readonly submitting = input.required<boolean>();
  readonly canSubmit = input.required<boolean>();
  readonly confirmText = input.required<string>();

  readonly confirmTextChange = output<string>();
  readonly submitted = output();

  protected readonly token = ARCHIVE_CONFIRM_TOKEN;
}
