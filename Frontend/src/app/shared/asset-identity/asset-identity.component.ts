import { ChangeDetectionStrategy, Component, computed, input, signal } from '@angular/core';

export type AssetIdentitySize = 'sm' | 'md' | 'lg';
export type AssetIdentityTone = 'default' | 'inverse';

const TRADINGVIEW_LOGO_BASE_URL = 'https://s3-symbol-logo.tradingview.com';
const MAX_SYMBOL_LENGTH = 24;
const MAX_FALLBACK_LENGTH = 4;
const SAFE_LOGO_SLUG = /^[a-z0-9]+(?:-[a-z0-9]+)*$/;

/**
 * TradingView logo IDs verified against the provider's symbol pages. Keep the
 * catalog explicit: guessing a slug sends an avoidable broken-image request.
 * TradingView attribution for this logo source is published at /legal/notices.
 */
const COMMON_TRADINGVIEW_LOGO_IDS: Readonly<Record<string, string>> = {
  AAPL: 'apple',
  ABBV: 'abbvie',
  ADBE: 'adobe',
  AMD: 'advanced-micro-devices',
  AMZN: 'amazon',
  ARKK: 'ark-etf-tr',
  AVGO: 'broadcom',
  BAC: 'bank-of-america',
  'BRK.B': 'berkshire-hathaway',
  BTCUSD: 'crypto/XTVCBTC',
  BTCUSDT: 'crypto/XTVCBTC',
  CAT: 'caterpillar',
  COIN: 'coinbase',
  COST: 'costco-wholesale',
  CRM: 'salesforce',
  CSCO: 'cisco',
  CVX: 'chevron',
  DIS: 'walt-disney',
  ETHUSD: 'crypto/XTVCETH',
  ETHUSDT: 'crypto/XTVCETH',
  F: 'ford',
  GE: 'ge-aerospace',
  GLD: 'spdr-gold-trust',
  GOOG: 'alphabet',
  GOOGL: 'alphabet',
  GS: 'golden-sachs-etf-trust-goldman',
  HOOD: 'robinhood',
  HYG: 'ishares',
  IBIT: 'ishares',
  INTC: 'intel',
  IWM: 'ishares',
  JNJ: 'johnson-and-johnson',
  JPM: 'jpmorgan-chase',
  KO: 'coca-cola',
  LLY: 'eli-lilly',
  MA: 'mastercard',
  META: 'meta-platforms',
  MRK: 'merck-and-co',
  MS: 'morgan-stanley',
  MSFT: 'microsoft',
  MU: 'micron-technology',
  NFLX: 'netflix',
  NKE: 'nike',
  NVDA: 'nvidia',
  ORCL: 'oracle',
  PEP: 'pepsico',
  PFE: 'pfizer',
  PG: 'procter-and-gamble',
  PLTR: 'palantir',
  PYPL: 'paypal',
  QQQ: 'invesco',
  SHOP: 'shopify',
  SLV: 'ishares',
  SMH: 'vaneck',
  SOXX: 'ishares',
  SPY: 'spdr-sandp500-etf-tr',
  SQ: 'block',
  T: 'at-and-t',
  TLT: 'ishares',
  TSLA: 'tesla',
  UBER: 'uber',
  UNH: 'unitedhealth',
  V: 'visa',
  VOO: 'vanguard',
  VTI: 'vanguard',
  VZ: 'verizon',
  WMT: 'walmart',
  XLB: 'sector/materials',
  XLE: 'sector/energy',
  XLF: 'sector/financial',
  XLI: 'sector/industrial',
  XLK: 'sector/technology',
  XLP: 'sector/consumer-staples',
  XLRE: 'sector/real-estate',
  XLU: 'sector/utilities',
  XLV: 'sector/health-care',
  XLY: 'sector/consumer-discretionary',
  XOM: 'exxon',
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

  const logoId = resolveTradingViewLogoId(symbol);
  return logoId ? `${TRADINGVIEW_LOGO_BASE_URL}/${logoId}.svg` : null;
}

export function resolveTradingViewLogoId(symbol: string): string | null {
  const root = rootSymbol(symbol);
  return root ? COMMON_TRADINGVIEW_LOGO_IDS[root] ?? null : null;
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
    '[attr.title]': 'showTitle() ? titleText() : null',
  },
})
export class AssetIdentityComponent {
  readonly symbol = input.required<string>();
  readonly name = input<string | null>(null);
  readonly exchange = input<string | null>(null);
  readonly logoSlug = input<string | null>(null);
  readonly logo = input<boolean>(true);
  readonly size = input<AssetIdentitySize>('md');
  readonly tone = input<AssetIdentityTone>('default');
  readonly showTitle = input<boolean>(true);

  private readonly failedLogoUrl = signal<string | null>(null);

  readonly displaySymbol = computed(() => displayAssetSymbol(this.symbol()));
  readonly displayName = computed(() => this.name()?.trim() || null);
  readonly logoUrl = computed(() => tradingViewLogoUrl(this.symbol(), this.logoSlug()));
  readonly showLogo = computed(() => {
    const logoUrl = this.logoUrl();
    return this.logo() && logoUrl !== null && logoUrl !== this.failedLogoUrl();
  });
  readonly showFallback = computed(() => this.logo() && !this.showLogo());
  readonly fallbackText = computed(() => fallbackTextForSymbol(this.symbol()));
  readonly displayExchange = computed(() => this.exchange()?.trim() || null);
  readonly titleText = computed(() => {
    const name = this.displayName();
    const symbol = this.displaySymbol();
    return name ? `${name} (${symbol})` : symbol;
  });

  onLogoError(): void {
    this.failedLogoUrl.set(this.logoUrl());
  }
}
