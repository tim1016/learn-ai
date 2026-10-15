import { ChangeDetectionStrategy, Component, computed, input } from '@angular/core';

import { OptionContractComponent, type OptionMarketDetails } from '../option-contract/option-contract.component';
import { optionContractFromSymbol } from '../option-contract/option-contract-symbol';
import { AssetWordmarkComponent, displayAssetSymbol, type AssetIdentitySize, type AssetIdentityTone } from './asset-wordmark.component';

export { resolveTradingViewLogoId, type AssetIdentitySize, type AssetIdentityTone } from './asset-wordmark.component';

/** The shared instrument renderer: contract chip for options, wordmark for everything else. */
@Component({
  selector: 'app-asset-identity',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [AssetWordmarkComponent, OptionContractComponent],
  templateUrl: './asset-identity.component.html',
  styleUrl: './asset-identity.component.scss',
  host: {
    class: 'asset-identity',
    '[class.asset-identity--xs]': 'size() === "xs"',
    '[class.asset-identity--sm]': 'size() === "sm"',
    '[class.asset-identity--lg]': 'size() === "lg"',
    '[class.asset-identity--inverse]': 'tone() === "inverse"',
    '[attr.title]': 'showTitle() && !optionContract() ? titleText() : null',
  },
})
export class AssetIdentityComponent {
  readonly symbol = input.required<string>();
  readonly name = input<string | null>(null);
  readonly exchange = input<string | null>(null);
  readonly logoSlug = input<string | null>(null);
  readonly logo = input(true);
  readonly size = input<AssetIdentitySize>('md');
  readonly tone = input<AssetIdentityTone>('default');
  readonly showTitle = input(true);
  readonly optionMarket = input<OptionMarketDetails | null>(null);
  readonly optionUnderlying = input<string | null>(null);

  protected readonly optionContract = computed(() => optionContractFromSymbol(this.symbol(), this.optionUnderlying()));
  protected readonly titleText = computed(() => {
    const symbol = displayAssetSymbol(this.symbol());
    const name = this.name()?.trim();
    return name ? `${name} (${symbol})` : symbol;
  });
}
