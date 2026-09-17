import { ChangeDetectionStrategy, Component, input } from '@angular/core';

import type { LaneModeChip } from '../../../services/alpaca-live-verdict.service';

/**
 * The one rendering of a `LaneModeChip` (`verdictModeChip`,
 * `alpaca-live-verdict.service.ts`): the tone-coloured pill, the "· N armed"
 * count and the "· Shadow" authority marker. The lane directory's card and
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
}
