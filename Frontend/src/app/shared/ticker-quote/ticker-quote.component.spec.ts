import { render, screen } from '@testing-library/angular';
import { describe, expect, it } from 'vitest';
import { SnapshotUnderlyingResult } from '../../graphql/types';
import { TickerQuoteComponent, TickerQuoteView } from './ticker-quote.component';

function requireElement<T extends Element>(node: T | null, selector: string): T {
  if (node === null) throw new Error(`expected ${selector} to render`);
  return node;
}

const BASE_QUOTE: TickerQuoteView = {
  ticker: 'SPY',
  name: 'SPDR S&P 500 ETF',
  exchange: 'ARCA',
  price: 542.44,
  changePercent: 0.77,
  logoSlug: 'spdr-s-p-500-etf-tr',
};

const NEGATIVE_QUOTE: TickerQuoteView = {
  ticker: 'QQQ',
  price: 471.1,
  changePercent: -0.42,
};

const FLAT_QUOTE: TickerQuoteView = {
  ticker: 'IWM',
  price: 200.0,
  changePercent: 0,
};

describe('TickerQuoteComponent', () => {
  describe('trend states', () => {
    it('applies .positive class and + sign prefix for positive changePercent', async () => {
      const { container } = await render(TickerQuoteComponent, {
        inputs: { quote: BASE_QUOTE },
      });

      const change = requireElement(
        container.querySelector('.ticker-quote__change'),
        '.ticker-quote__change',
      );
      expect(change.classList.contains('positive')).toBe(true);
      expect(change.classList.contains('negative')).toBe(false);
      expect(change.textContent?.trim()).toMatch(/^\+/);
    });

    it('applies .negative class and - sign for negative changePercent', async () => {
      const { container } = await render(TickerQuoteComponent, {
        inputs: { quote: NEGATIVE_QUOTE },
      });

      const change = requireElement(
        container.querySelector('.ticker-quote__change'),
        '.ticker-quote__change',
      );
      expect(change.classList.contains('negative')).toBe(true);
      expect(change.classList.contains('positive')).toBe(false);
      expect(change.textContent?.trim()).toMatch(/-/);
    });

    it('applies .flat class and no sign for zero changePercent', async () => {
      const { container } = await render(TickerQuoteComponent, {
        inputs: { quote: FLAT_QUOTE },
      });

      const change = requireElement(
        container.querySelector('.ticker-quote__change'),
        '.ticker-quote__change',
      );
      expect(change.classList.contains('flat')).toBe(true);
      expect(change.classList.contains('positive')).toBe(false);
      expect(change.classList.contains('negative')).toBe(false);
      expect(change.textContent?.trim()).not.toMatch(/^\+/);
    });

    it('renders an em dash when changePercent is unavailable', async () => {
      const { container } = await render(TickerQuoteComponent, {
        inputs: { quote: { ...BASE_QUOTE, changePercent: null } },
      });

      const change = requireElement(
        container.querySelector('.ticker-quote__change'),
        '.ticker-quote__change',
      );
      expect(change.textContent?.trim()).toBe('—');
    });

    it('shows pi-caret-up for positive in card mode', async () => {
      const { container } = await render(TickerQuoteComponent, {
        inputs: { quote: BASE_QUOTE, mode: 'card' },
      });

      const caret = requireElement(container.querySelector('i'), 'i');
      expect(caret.classList.contains('pi-caret-up')).toBe(true);
    });

    it('shows pi-caret-down for negative in card mode', async () => {
      const { container } = await render(TickerQuoteComponent, {
        inputs: { quote: NEGATIVE_QUOTE, mode: 'card' },
      });

      const caret = requireElement(container.querySelector('i'), 'i');
      expect(caret.classList.contains('pi-caret-down')).toBe(true);
    });

    it('shows pi-minus for flat in card mode', async () => {
      const { container } = await render(TickerQuoteComponent, {
        inputs: { quote: FLAT_QUOTE, mode: 'card' },
      });

      const caret = requireElement(container.querySelector('i'), 'i');
      expect(caret.classList.contains('pi-minus')).toBe(true);
    });
  });

  describe('price and currency formatting', () => {
    it('renders price with default $ prefix and 2 decimal places', async () => {
      const { container } = await render(TickerQuoteComponent, {
        inputs: { quote: BASE_QUOTE },
      });

      const price = requireElement(
        container.querySelector('.ticker-quote__price'),
        '.ticker-quote__price',
      );
      expect(price.textContent?.trim()).toContain('$');
      expect(price.textContent?.trim()).toContain('542.44');
    });

    it('accepts a market snapshot without an adapter', async () => {
      const snapshot: SnapshotUnderlyingResult = {
        ticker: 'DIA',
        price: 4200.5,
        change: 12.34,
        changePercent: 0.29,
      };
      const quote: TickerQuoteView = snapshot;
      const { container } = await render(TickerQuoteComponent, {
        inputs: { quote },
      });

      expect(container.textContent).toContain('DIA');
    });

    it('uses a custom currencySymbol when provided', async () => {
      const quote: TickerQuoteView = { ...BASE_QUOTE, currencySymbol: '€' };
      const { container } = await render(TickerQuoteComponent, {
        inputs: { quote },
      });

      const price = requireElement(
        container.querySelector('.ticker-quote__price'),
        '.ticker-quote__price',
      );
      expect(price.textContent?.trim()).toContain('€');
      expect(price.textContent?.trim()).not.toContain('$');
    });

    it('formats percent with 2 decimal places', async () => {
      const quote: TickerQuoteView = { ...BASE_QUOTE, changePercent: 0.77 };
      const { container } = await render(TickerQuoteComponent, {
        inputs: { quote },
      });

      const change = requireElement(
        container.querySelector('.ticker-quote__change'),
        '.ticker-quote__change',
      );
      expect(change.textContent?.trim()).toContain('0.77%');
    });
  });

  describe('inline mode (default)', () => {
    it('honors an explicitly requested identity size', async () => {
      const { container } = await render(TickerQuoteComponent, {
        inputs: { quote: BASE_QUOTE, size: 'lg' },
      });

      expect(container.querySelector('app-asset-identity.asset-identity--lg')).not.toBeNull();
    });

    it('does not render name in inline mode', async () => {
      await render(TickerQuoteComponent, {
        inputs: { quote: BASE_QUOTE },
      });

      expect(screen.queryByText('SPDR S&P 500 ETF')).toBeNull();
    });

    it('does not render exchange chip in inline mode', async () => {
      const { container } = await render(TickerQuoteComponent, {
        inputs: { quote: BASE_QUOTE },
      });

      expect(container.querySelector('.asset-identity__exchange')).toBeNull();
    });

    it('does not render caret icon in inline mode', async () => {
      const { container } = await render(TickerQuoteComponent, {
        inputs: { quote: BASE_QUOTE },
      });

      expect(container.querySelector('i')).toBeNull();
    });

    it('host has inline class', async () => {
      const { fixture } = await render(TickerQuoteComponent, {
        inputs: { quote: BASE_QUOTE },
      });

      expect(
        fixture.nativeElement.classList.contains('ticker-quote--inline'),
      ).toBe(true);
    });
  });

  describe('card mode', () => {
    it('renders name in card mode', async () => {
      await render(TickerQuoteComponent, {
        inputs: { quote: BASE_QUOTE, mode: 'card' },
      });

      expect(screen.getByText('SPDR S&P 500 ETF')).toBeTruthy();
    });

    it('renders exchange chip in card mode when exchange is provided', async () => {
      const { container } = await render(TickerQuoteComponent, {
        inputs: { quote: BASE_QUOTE, mode: 'card' },
      });

      const chip = requireElement(
        container.querySelector('.asset-identity__exchange'),
        '.asset-identity__exchange',
      );
      expect(chip.textContent?.trim()).toBe('ARCA');
    });

    it('does not render exchange chip in card mode when exchange is absent', async () => {
      const { container } = await render(TickerQuoteComponent, {
        inputs: { quote: NEGATIVE_QUOTE, mode: 'card' },
      });

      expect(container.querySelector('.asset-identity__exchange')).toBeNull();
    });

    it('renders caret in card mode', async () => {
      const { container } = await render(TickerQuoteComponent, {
        inputs: { quote: BASE_QUOTE, mode: 'card' },
      });

      expect(container.querySelector('i')).not.toBeNull();
    });

    it('host has card class', async () => {
      const { fixture } = await render(TickerQuoteComponent, {
        inputs: { quote: BASE_QUOTE, mode: 'card' },
      });

      expect(
        fixture.nativeElement.classList.contains('ticker-quote--card'),
      ).toBe(true);
    });

    it('keeps the rich ticker tooltip when hovering the identity', async () => {
      const { container, fixture } = await render(TickerQuoteComponent, {
        inputs: { quote: { ...BASE_QUOTE, change: 4.16 }, mode: 'card' },
      });

      expect(fixture.nativeElement.getAttribute('title')).toBe(
        'SPDR S&P 500 ETF (SPY) — $542.44 (+$4.16), +0.77%',
      );
      expect(
        container.querySelector('app-asset-identity')?.getAttribute('title'),
      ).toBeNull();
    });
  });

  describe('logo input', () => {
    it('renders no logo element when logo=false', async () => {
      const { container } = await render(TickerQuoteComponent, {
        inputs: { quote: BASE_QUOTE, logo: false },
      });

      expect(container.querySelector('img')).toBeNull();
      expect(container.querySelector('.asset-identity__fallback')).toBeNull();
    });

    it('renders a logo by default when logoSlug is provided', async () => {
      const { container } = await render(TickerQuoteComponent, {
        inputs: { quote: BASE_QUOTE },
      });

      expect(container.querySelector('img')).not.toBeNull();
    });
  });

  describe('accessibility', () => {
    it('caret icon has aria-hidden in card mode', async () => {
      const { container } = await render(TickerQuoteComponent, {
        inputs: { quote: BASE_QUOTE, mode: 'card' },
      });

      const caret = requireElement(container.querySelector('i'), 'i');
      expect(caret.getAttribute('aria-hidden')).toBe('true');
    });

    it('change span has aria-label for positive trend', async () => {
      const { container } = await render(TickerQuoteComponent, {
        inputs: { quote: BASE_QUOTE },
      });

      const change = requireElement(
        container.querySelector('.ticker-quote__change'),
        '.ticker-quote__change',
      );
      const label = change.getAttribute('aria-label');
      expect(label).toContain('up');
      expect(label).toContain('percent');
    });

    it('change span has aria-label for negative trend', async () => {
      const { container } = await render(TickerQuoteComponent, {
        inputs: { quote: NEGATIVE_QUOTE },
      });

      const change = requireElement(
        container.querySelector('.ticker-quote__change'),
        '.ticker-quote__change',
      );
      expect(change.getAttribute('aria-label')).toContain('down');
    });

    it('change span has aria-label for flat trend', async () => {
      const { container } = await render(TickerQuoteComponent, {
        inputs: { quote: FLAT_QUOTE },
      });

      const change = requireElement(
        container.querySelector('.ticker-quote__change'),
        '.ticker-quote__change',
      );
      expect(change.getAttribute('aria-label')).toBe('unchanged');
    });
  });

  describe('host title', () => {
    it('includes the absolute change and grouped price when present', async () => {
      const { fixture } = await render(TickerQuoteComponent, {
        inputs: {
          quote: {
            ...BASE_QUOTE,
            price: 1542.44,
            change: -12.34,
            changePercent: -0.8,
          },
        },
      });

      expect(fixture.nativeElement.getAttribute('title')).toBe(
        'SPDR S&P 500 ETF (SPY) — $1,542.44 (-$12.34), -0.80%',
      );
    });
  });
});
