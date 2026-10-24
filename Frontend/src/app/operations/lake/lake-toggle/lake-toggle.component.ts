import { ChangeDetectionStrategy, Component, input, output } from '@angular/core';

import { ReceiptLabelPipe } from '../../../shared/pipes/receipt-label.pipe';

/** A pressed button per option: which of a few views the page is showing. */
@Component({
  selector: 'app-lake-toggle',
  changeDetection: ChangeDetectionStrategy.OnPush,
  styleUrl: './lake-toggle.component.scss',
  imports: [ReceiptLabelPipe],
  template: `
    <div class="toggle" role="group" [attr.aria-label]="label()">
      @for (option of options(); track option) {
        <button
          type="button"
          class="toggle__option"
          [class.toggle__option--on]="option === value()"
          [attr.aria-pressed]="option === value()"
          (click)="picked.emit(option)"
        >
          {{ option | receiptLabel }}
        </button>
      }
    </div>
  `,
})
export class LakeToggleComponent<T extends string> {
  /** What the options choose between; the group's accessible name. */
  readonly label = input.required<string>();
  /** Backend identifiers, rendered through the receipt-label pipe. */
  readonly options = input.required<readonly T[]>();
  readonly value = input.required<T>();

  readonly picked = output<T>();
}
