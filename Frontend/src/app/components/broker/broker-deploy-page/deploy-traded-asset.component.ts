import { ChangeDetectionStrategy, Component, computed, input, linkedSignal, output, signal } from '@angular/core';

import { SymbolPickerComponent } from '../../../shared/symbol-picker/symbol-picker.component';
import type { TradedAssetWarning } from './deploy-traded-acknowledgement.component';

type TradedChoice = 'same' | 'stock';

/**
 * Deploy's "What it trades" line (#2912), under the symbol the bot listens to.
 *
 * Closed by default: a bot trades the symbol it listens to unless the owner
 * opens the line and picks something else. Choosing "Same as signal" clears
 * what was picked. The summary, the warning and the refusal are the
 * backend's sentences; this component writes none and never compares the two
 * tickers itself.
 */
@Component({
  selector: 'app-deploy-traded-asset',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [SymbolPickerComponent],
  templateUrl: './deploy-traded-asset.component.html',
  styleUrl: './deploy-traded-asset.component.scss',
})
export class DeployTradedAssetComponent {
  /** What the bot trades when that is not the symbol it listens to; `null` is the same symbol. */
  readonly tradedSymbol = input<string | null>(null);
  /** One line saying what the bot trades, from the backend; `null` while it is being read. */
  readonly summary = input<string | null>(null);
  readonly warning = input<TradedAssetWarning | null>(null);
  /** Why the picked asset cannot be traded, from the backend. */
  readonly refusal = input<string | null>(null);

  readonly tradedSymbolChange = output<string | null>();

  /** Open once the owner opens it, and whenever the bot already trades something else. */
  private readonly opened = signal(false);
  protected readonly open = computed(() => this.opened() || this.tradedSymbol() !== null);

  /** "Another stock or ETF" stays chosen while its picker is still empty. */
  protected readonly choice = linkedSignal<TradedChoice>(() => (this.tradedSymbol() === null ? 'same' : 'stock'));

  protected toggle(): void {
    this.opened.update((open) => !open);
  }

  protected choose(choice: TradedChoice): void {
    this.choice.set(choice);
    if (choice === 'same' && this.tradedSymbol() !== null) this.tradedSymbolChange.emit(null);
  }

  protected pick(symbol: string): void {
    const picked = symbol.trim().toUpperCase();
    this.tradedSymbolChange.emit(picked === '' ? null : picked);
  }
}
