import { render, screen } from '@testing-library/angular';
import { describe, expect, it } from 'vitest';
import { AssetIdentityComponent } from './asset-identity.component';

function requireElement<T extends Element>(node: T | null, selector: string): T {
  if (node === null) throw new Error(`expected ${selector} to render`);
  return node;
}

describe('AssetIdentityComponent', () => {
  it('renders a known TradingView logo beside the ticker and asset name', async () => {
    const { container } = await render(AssetIdentityComponent, {
      inputs: { symbol: 'AAPL', name: 'Apple Inc.' },
    });

    const logo = requireElement(container.querySelector<HTMLImageElement>('img'), 'img');
    expect(logo.getAttribute('src')).toBe(
      'https://s3-symbol-logo.tradingview.com/apple.svg',
    );
    expect(screen.getByText('AAPL')).toBeTruthy();
    expect(screen.getByText('Apple Inc.')).toBeTruthy();
  });

  it('allows explicit TradingView slugs', async () => {
    const { container } = await render(AssetIdentityComponent, {
      inputs: { symbol: 'BRK.B', logoSlug: 'berkshire-hathaway' },
    });

    const logo = requireElement(container.querySelector<HTMLImageElement>('img'), 'img');
    expect(logo.getAttribute('src')).toBe(
      'https://s3-symbol-logo.tradingview.com/berkshire-hathaway.svg',
    );
  });

  it('uses a text fallback when the logo slug is invalid', async () => {
    const { container } = await render(AssetIdentityComponent, {
      inputs: { symbol: 'AAPL', logoSlug: 'https://example.invalid/logo.svg' },
    });

    expect(container.querySelector('img')).toBeNull();
    const fallback = requireElement(
      container.querySelector<HTMLElement>('.asset-identity__fallback'),
      '.asset-identity__fallback',
    );
    expect(fallback.textContent).toBe('AAPL');
  });

  it('blocks script-like logo slugs instead of falling back to a known symbol URL', async () => {
    const { container } = await render(AssetIdentityComponent, {
      inputs: { symbol: 'AAPL', logoSlug: 'javascript:alert(1)' },
    });

    expect(container.querySelector('img')).toBeNull();
    const fallback = requireElement(
      container.querySelector<HTMLElement>('.asset-identity__fallback'),
      '.asset-identity__fallback',
    );
    expect(fallback.textContent).toBe('AAPL');
  });

  it('hides a broken image and keeps the symbol visible', async () => {
    const { container, fixture } = await render(AssetIdentityComponent, {
      inputs: { symbol: 'MSFT', logoSlug: 'microsoft' },
    });
    const logo = requireElement(container.querySelector<HTMLImageElement>('img'), 'img');

    logo.dispatchEvent(new Event('error'));
    fixture.detectChanges();

    expect(container.querySelector('img')).toBeNull();
    const fallback = requireElement(
      container.querySelector<HTMLElement>('.asset-identity__fallback'),
      '.asset-identity__fallback',
    );
    expect(fallback.textContent).toBe('MSFT');
    const symbol = requireElement(
      container.querySelector<HTMLElement>('.asset-identity__symbol'),
      '.asset-identity__symbol',
    );
    expect(symbol.textContent).toBe('MSFT');
  });
});
