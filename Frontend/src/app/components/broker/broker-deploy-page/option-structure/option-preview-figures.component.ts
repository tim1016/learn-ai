import { ChangeDetectionStrategy, Component, computed, input } from '@angular/core';

import type { OptionStructurePreview } from '../../v2-panel/lib/broker-v2-panel.service';

/**
 * The figures of a resolved option structure preview (#2915): which way it
 * leans, the at-expiry estimate, what it gains or loses with a day, and the
 * money. Every sentence is the server's, and so is every dollar string:
 * nothing is written out or added up here. A premium carries no sign: the
 * server's word beside it (`premium_is_label`) says paid or received.
 *
 * `compact` is Deploy's short view: the premium a share and a structure,
 * the lean and the estimate, and the cost of opening and closing at once
 * only when it is over the owner's limit.
 */
@Component({
  selector: 'app-option-preview-figures',
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './option-preview-figures.component.html',
  styleUrl: './option-preview-figures.component.scss',
})
export class OptionPreviewFiguresComponent {
  /** A resolved answer. */
  readonly preview = input.required<OptionStructurePreview>();
  readonly compact = input(false);

  protected readonly money = computed(() => this.preview().money);
}
