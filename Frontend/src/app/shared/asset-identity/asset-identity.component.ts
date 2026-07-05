import { ChangeDetectionStrategy, Component, computed, input, signal } from '@angular/core';

export type AssetIdentitySize = 'sm' | 'md' | 'lg';
export type AssetIdentityTone = 'default' | 'inverse';

const TRADINGVIEW_LOGO_BASE_URL = 'https://s3-symbol-logo.tradingview.com';
const MAX_SYMBOL_LENGTH = 24;
const MAX_FALLBACK_LENGTH = 4;
const SAFE_LOGO_SLUG = /^[a-z0-9]+(?:-[a-z0-9]+)*$/;

const KNOWN_TRADINGVIEW_LOGO_SLUGS: Readonly<Record<string, string>> = {
  AAPL: 'apple',
  AMD: 'advanced-micro-devices',
  AMZN: 'amazon',
  GOOGL: 'alphabet',
  IWM: 'ishares-russell-2000-etf',
  META: 'meta-platforms',
  MSFT: 'microsoft',
  NVDA: 'nvidia',
  QQQ: 'invesco-qqq-trust',
  SPY: 'spdr-s-p-500-etf-tr',
  TSLA: 'tesla',
};

function normalizeAssetSymbol(raw: string): string {
  return raw
    .trim()
    .toUpperCase()
    .replace(/\s+/g, '')
    .replace(/[^A-Z0-9.:-]/g, '')
    .slice(0, MAX_SYMBOL_LENGTH);
}

function displayAssetSymbol(raw: string): string {
  return normalizeAssetSymbol(raw) || 'ASSET';
}

function normalizeTradingViewLogoSlug(raw: string | null | undefined): string | null {
  const slug = raw?.trim().toLowerCase() ?? '';
  return SAFE_LOGO_SLUG.test(slug) ? slug : null;
}

function tradingViewLogoUrl(
  symbol: string,
  explicitLogoSlug?: string | null,
): string | null {
  if (explicitLogoSlug !== undefined && explicitLogoSlug !== null) {
    const explicit = normalizeTradingViewLogoSlug(explicitLogoSlug);
    return explicit ? `${TRADINGVIEW_LOGO_BASE_URL}/${explicit}.svg` : null;
  }

  const slug = logoSlugForSymbol(symbol);
  return slug ? `${TRADINGVIEW_LOGO_BASE_URL}/${slug}.svg` : null;
}

function logoSlugForSymbol(symbol: string): string | null {
  const root = rootSymbol(symbol);
  if (!root) return null;

  const known = KNOWN_TRADINGVIEW_LOGO_SLUGS[root];
  if (known) return known;

  return normalizeTradingViewLogoSlug(
    root
      .toLowerCase()
      .replace(/[^a-z0-9]+/g, '-')
      .replace(/^-+|-+$/g, ''),
  );
}

function rootSymbol(symbol: string): string {
  const normalized = normalizeAssetSymbol(symbol);
  const root = normalized.includes(':') ? normalized.split(':').at(-1) ?? '' : normalized;
  return root.replace(/^[.-]+|[.-]+$/g, '');
}

function fallbackTextForSymbol(symbol: string): string {
  const cleaned = rootSymbol(symbol).replace(/[^A-Z0-9]/g, '');
  return (cleaned || 'AS').slice(0, MAX_FALLBACK_LENGTH);
}

@Component({
  selector: 'app-asset-identity',
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './asset-identity.component.html',
  styleUrl: './asset-identity.component.scss',
  host: {
    class: 'asset-identity',
    '[class.asset-identity--sm]': 'size() === "sm"',
    '[class.asset-identity--lg]': 'size() === "lg"',
    '[class.asset-identity--inverse]': 'tone() === "inverse"',
    '[attr.title]': 'titleText()',
  },
})
export class AssetIdentityComponent {
  readonly symbol = input.required<string>();
  readonly name = input<string | null>(null);
  readonly logoSlug = input<string | null>(null);
  readonly size = input<AssetIdentitySize>('md');
  readonly tone = input<AssetIdentityTone>('default');

  private readonly failedLogoUrl = signal<string | null>(null);

  readonly displaySymbol = computed(() => displayAssetSymbol(this.symbol()));
  readonly displayName = computed(() => this.name()?.trim() || null);
  readonly logoUrl = computed(() => tradingViewLogoUrl(this.symbol(), this.logoSlug()));
  readonly showLogo = computed(() => {
    const logoUrl = this.logoUrl();
    return logoUrl !== null && logoUrl !== this.failedLogoUrl();
  });
  readonly fallbackText = computed(() => fallbackTextForSymbol(this.symbol()));
  readonly titleText = computed(() => {
    const name = this.displayName();
    const symbol = this.displaySymbol();
    return name ? `${name} (${symbol})` : symbol;
  });

  onLogoError(): void {
    this.failedLogoUrl.set(this.logoUrl());
  }
}
