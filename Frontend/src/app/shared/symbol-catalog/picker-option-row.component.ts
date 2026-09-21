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
  template: `
    <app-asset-identity
      class="row__identity"
      [symbol]="symbol()"
      size="xs"
      [showTitle]="false"
    />
    <span class="row__name">{{ name() }}</span>
    <span class="row__meta mono">{{ exchange() ?? '—' }}</span>
    <span class="row__held mono" [class.row__held--unheld]="!lastHeld()">
      {{ heldCopy() }}
    </span>
  `,
  styles: `
    :host {
      display: contents;
    }

    .row__identity {
      min-width: 0;
    }

    .row__name {
      font-size: 11px;
      color: var(--text-subtle);
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }

    .row__meta {
      font-size: 9px;
      color: var(--text-muted);
      text-align: right;
    }

    .row__held {
      font-size: 10px;
      color: var(--text-muted);
      text-align: right;
    }

    .row__held--unheld {
      color: var(--text-subtle);
      font-style: italic;
    }
  `,
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
