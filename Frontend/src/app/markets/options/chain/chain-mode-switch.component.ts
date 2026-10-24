import { ChangeDetectionStrategy, Component, computed, input, output } from '@angular/core';

import { CHAIN_MODES, chainModeName, type ChainMode, type ChainModeId } from './chain-modes';

/**
 * The four ways to ask for a chain, as one strip: Now (Snapshot, Live IBKR) and Past (Archive,
 * Live IBKR). A mode that cannot be chosen stays on the strip, disabled, with its reason in
 * words beside it; choosing never falls back to another mode's chain.
 */
@Component({
  selector: 'app-chain-mode-switch',
  templateUrl: './chain-mode-switch.component.html',
  styleUrl: './chain-mode-switch.component.scss',
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class ChainModeSwitchComponent {
  /** The mode the address asks for, or `null` when it names none. */
  readonly active = input<ChainModeId | null>(null);
  /** The mode the operator chose. Only a mode that can be chosen is ever emitted. */
  readonly picked = output<ChainMode>();

  protected readonly groups = (['Now', 'Past'] as const).map((when) => ({
    when,
    modes: CHAIN_MODES.filter((mode) => mode.when === when),
  }));
  protected readonly reasons = computed(() =>
    CHAIN_MODES.flatMap((mode) => (mode.unavailable === null ? [] : [mode.unavailable])),
  );

  protected nameOf(id: ChainModeId): string {
    return chainModeName(id);
  }
}
