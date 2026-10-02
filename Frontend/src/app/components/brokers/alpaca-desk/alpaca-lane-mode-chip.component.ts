import { ChangeDetectionStrategy, Component, computed, input } from '@angular/core';

import type { LaneModeChip } from '../../../services/alpaca-live-verdict.service';

/**
 * The one rendering of a `LaneModeChip` (`verdictModeChip`,
 * `alpaca-live-verdict.service.ts`): one world label and its tone. The lane directory's card and
 * the account workspace's header each hand-rolled a byte-identical copy of
 * this markup and palette (#2185) — this is now the only place either
 * exists, so a tone or a wording change can never drift between them again.
 */
@Component({
  selector: 'app-alpaca-lane-mode-chip',
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './alpaca-lane-mode-chip.component.html',
  styleUrl: './alpaca-lane-mode-chip.component.scss',
})
export class AlpacaLaneModeChipComponent {
  readonly chip = input.required<LaneModeChip>();
  /** `badge` is the workspace header's mode badge (PRD #2560 D4): the same
   * words, filled with the lane colour. `stacked` is that badge on a bot's
   * page, where the header is short of room: the world over its detail,
   * "LIVE" over "real money". */
  readonly variant = input<'chip' | 'badge' | 'stacked'>('chip');

  /** The mode split at its " · " for the stacked badge; `null` keeps one line,
   * as for a mode still being read, which has no detail to stack. */
  protected readonly stackedWords = computed(() => {
    if (this.variant() !== 'stacked') return null;
    const [world, ...detail] = this.chip().mode.split(' · ');
    return detail.length > 0 ? { world, detail: detail.join(' · ') } : null;
  });
}
