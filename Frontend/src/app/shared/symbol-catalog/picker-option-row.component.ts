import { ChangeDetectionStrategy, Component, computed, input } from '@angular/core';

import { AssetIdentityComponent } from '../asset-identity';

/**
 * One option row's content — the shared presentation of a pickable symbol
 * across the picker family (single card, multi card, backfill panel).
 *
 * The wrapper (a `<button class="row">`, an `<li>` suggestion…) belongs to
 * the host; this component renders only the four grid cells. Its host
 * element is `display: contents`, so the cells are the wrapper's direct
 * grid children and no host needs to re-declare the row's column layout.
 */
@Component({
  selector: 'app-picker-option-row',
  imports: [AssetIdentityComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './picker-option-row.component.html',
  styleUrl: './picker-option-row.component.scss',
})
export class PickerOptionRowComponent {
  readonly symbol = input.required<string>();
  readonly name = input.required<string>();
  readonly exchange = input<string | null | undefined>(undefined);
  readonly lastHeld = input<string | null>(null);
  readonly delisted = input(false);

  /** The row's right-hand coverage copy: the strongest fact about the lake. */
  readonly heldCopy = computed(() =>
    this.delisted() ? 'delisted' : (this.lastHeld() ?? 'not held'),
  );
}
