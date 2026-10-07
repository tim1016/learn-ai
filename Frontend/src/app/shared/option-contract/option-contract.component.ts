import { ChangeDetectionStrategy, Component, computed, input, signal } from '@angular/core';

import { AssetIdentityComponent } from '../asset-identity/asset-identity.component';
import { formatTimestampDisplay } from '../timestamp/timestamp-display';

/** The facts that name one option contract, as the server sends them (#2913). */
export interface OptionContract {
  /** The contract's code at the broker, e.g. `SPY261009C00585000`. */
  occ_symbol: string;
  underlying: string;
  /** The expiry date's 16:00 New York anchor, `int64 ms UTC` (ADR 0022 (i)). */
  expiry_ms: number;
  right: 'call' | 'put';
  /** Dollars: the exact decimal the server sends (`"587.5"`), or a number. */
  strike: string | number;
}

const RIGHT_IN_WORDS: Record<OptionContract['right'], string> = { call: 'Call', put: 'Put' };

/**
 * A strike with no zeros it does not need: `585`, `587.5`. Only a plain
 * decimal is trimmed, and as text, so an exact strike never passes through a
 * float; anything else is shown as it came.
 */
function strikeText(strike: string | number): string {
  const text = String(strike);
  return /^\d+\.\d+$/.test(text) ? text.replace(/\.?0+$/, '') : text;
}

/**
 * A contract's name after its underlying: `Oct 9 '26 585 Call`. The year is
 * always there, and the expiry is read as a date in New York, so it is the
 * same date in every viewer's zone.
 */
function contractTerms(contract: OptionContract): string {
  const expiry = formatTimestampDisplay(contract.expiry_ms, { mode: 'date-et', granularity: 'short-date' });
  return `${expiry} ${strikeText(contract.strike)} ${RIGHT_IN_WORDS[contract.right]}`;
}

/**
 * A contract's name as a trader says it: `SPY Oct 9 '26 585 Call`. For an
 * accessible name, or a place no component can sit; on screen a contract is
 * shown by {@link OptionContractComponent}.
 */
export function optionContractName(contract: OptionContract): string {
  return `${contract.underlying} ${contractTerms(contract)}`;
}

let nextCodeId = 0;

/**
 * The one renderer for an option contract's name (#2915). Every page that
 * shows a contract uses it; none formats one itself. The underlying is a
 * tradeable symbol, so the shared symbol renderer draws it. The raw contract
 * code is one click away, for matching a contract at the broker.
 */
@Component({
  selector: 'app-option-contract',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [AssetIdentityComponent],
  templateUrl: './option-contract.component.html',
  styleUrl: './option-contract.component.scss',
})
export class OptionContractComponent {
  readonly contract = input.required<OptionContract>();

  protected readonly codeShown = signal(false);
  protected readonly codeId = `option-contract-code-${nextCodeId++}`;
  protected readonly terms = computed(() => contractTerms(this.contract()));
  protected readonly revealLabel = computed(
    () => `${this.codeShown() ? 'Hide' : 'Show'} contract code for ${optionContractName(this.contract())}`,
  );
}
