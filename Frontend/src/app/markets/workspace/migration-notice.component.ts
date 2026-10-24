import { ChangeDetectionStrategy, Component, input, output } from '@angular/core';

/**
 * What a page ignored of an address or a saved setup it was handed (PRD #3490
 * R10, R33): said where the person is looking, never dropped without a word.
 * Presentation only; the host decides what was ignored and what dismissing does.
 */
@Component({
  selector: 'app-migration-notice',
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <div class="migration-notice" role="status">
      <div class="migration-notice__text">
        <strong>{{ heading() }}</strong>
        <ul>
          @for (note of notes(); track note) {
            <li>{{ note }}</li>
          }
        </ul>
      </div>
      <button type="button" class="migration-notice__dismiss" (click)="dismissed.emit()">Dismiss</button>
    </div>
  `,
  styleUrl: './migration-notice.component.scss',
})
export class MigrationNoticeComponent {
  readonly heading = input.required<string>();
  readonly notes = input.required<readonly string[]>();
  readonly dismissed = output();
}
