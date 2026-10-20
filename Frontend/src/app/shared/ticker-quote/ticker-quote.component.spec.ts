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
    it('renders a negative quote inline: .negative class with a minus sign, and a down aria-label', async () => {
      const { container } = await render(TickerQuoteComponent, {
        inputs: { quote: NEGATIVE_QUOTE },
      });

      const change = requireElement(
        container.querySelector('.ticker-quote__change'),
        '.ticker-quote__change',
      );
      expect.soft(change.classList.contains('negative'), 'applies .negative class for negative changePercent').toBe(true);
      expect.soft(change.classList.contains('positive'), 'applies .negative class, not .positive, for negative changePercent').toBe(false);
      expect.soft(change.textContent?.trim(), 'shows a - sign for negative changePercent').toMatch(/-/);
      expect.soft(change.getAttribute('aria-label'), 'change span has aria-label for negative trend').toContain('down');
    });

    it('renders a flat quote inline: .flat class with no sign, and an unchanged aria-label', async () => {
      const { container } = await render(TickerQuoteComponent, {
        inputs: { quote: FLAT_QUOTE },
      });

      const change = requireElement(
        container.querySelector('.ticker-quote__change'),
        '.ticker-quote__change',
      );
      expect.soft(change.classList.contains('flat'), 'applies .flat class for zero changePercent').toBe(true);
      expect.soft(change.classList.contains('positive'), 'applies .flat class, not .positive, for zero changePercent').toBe(false);
      expect.soft(change.classList.contains('negative'), 'applies .flat class, not .negative, for zero changePercent').toBe(false);
      expect.soft(change.textContent?.trim(), 'shows no + sign for zero changePercent').not.toMatch(/^\+/);
      expect.soft(change.getAttribute('aria-label'), 'change span has aria-label for flat trend').toBe('unchanged');
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

    it('shows pi-minus for flat in card mode', async () => {
      const { container } = await render(TickerQuoteComponent, {
        inputs: { quote: FLAT_QUOTE, mode: 'card' },
      });

      const caret = requireElement(container.querySelector('i'), 'i');
      expect(caret.classList.contains('pi-minus')).toBe(true);
    });
  });

  describe('price and currency formatting', () => {
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
  });

  describe('inline mode (default)', () => {
    it('honors an explicitly requested identity size', async () => {
      const { container } = await render(TickerQuoteComponent, {
        inputs: { quote: BASE_QUOTE, size: 'lg' },
      });

      expect(container.querySelector('app-asset-identity.asset-identity--lg')).not.toBeNull();
    });

    it('renders a positive quote inline: .positive class with a plus sign, price and percent, a logo, an up aria-label, and no name, exchange chip or caret', async () => {
      const { container } = await render(TickerQuoteComponent, {
        inputs: { quote: BASE_QUOTE },
      });

      const change = requireElement(
        container.querySelector('.ticker-quote__change'),
        '.ticker-quote__change',
      );
      expect.soft(change.classList.contains('positive'), 'applies .positive class for positive changePercent').toBe(true);
      expect.soft(change.classList.contains('negative'), 'applies .positive class, not .negative, for positive changePercent').toBe(false);
      expect.soft(change.textContent?.trim(), 'shows a + sign prefix for positive changePercent').toMatch(/^\+/);
      expect.soft(change.textContent?.trim(), 'formats percent with 2 decimal places').toContain('0.77%');
      const price = requireElement(
        container.querySelector('.ticker-quote__price'),
        '.ticker-quote__price',
      );
      expect.soft(price.textContent?.trim(), 'renders price with default $ prefix').toContain('$');
      expect.soft(price.textContent?.trim(), 'renders price with 2 decimal places').toContain('542.44');
      expect.soft(screen.queryByText('SPDR S&P 500 ETF'), 'does not render name in inline mode').toBeNull();
      expect.soft(container.querySelector('.asset-identity__exchange'), 'does not render exchange chip in inline mode').toBeNull();
      expect.soft(container.querySelector('i'), 'does not render caret icon in inline mode').toBeNull();
      expect.soft(container.querySelector('img'), 'renders a logo by default when logoSlug is provided').not.toBeNull();
      const label = change.getAttribute('aria-label');
      expect.soft(label, 'change span has aria-label for positive trend: up').toContain('up');
      expect.soft(label, 'change span has aria-label for positive trend: percent').toContain('percent');
    });
  });

  describe('card mode', () => {
    it('renders a positive quote as a card: a caret up, the name, and the exchange chip', async () => {
      const { container } = await render(TickerQuoteComponent, {
        inputs: { quote: BASE_QUOTE, mode: 'card' },
      });

      const caret = requireElement(container.querySelector('i'), 'i');
      expect.soft(caret.classList.contains('pi-caret-up'), 'shows pi-caret-up for positive in card mode').toBe(true);
      expect.soft(screen.getByText('SPDR S&P 500 ETF'), 'renders name in card mode').toBeTruthy();
      const chip = requireElement(
        container.querySelector('.asset-identity__exchange'),
        '.asset-identity__exchange',
      );
      expect.soft(chip.textContent?.trim(), 'renders exchange chip in card mode when exchange is provided').toBe('ARCA');
    });

    it('renders a negative quote without an exchange as a card: a caret down and no exchange chip', async () => {
      const { container } = await render(TickerQuoteComponent, {
        inputs: { quote: NEGATIVE_QUOTE, mode: 'card' },
      });

      const caret = requireElement(container.querySelector('i'), 'i');
      expect.soft(caret.classList.contains('pi-caret-down'), 'shows pi-caret-down for negative in card mode').toBe(true);
      expect.soft(container.querySelector('.asset-identity__exchange'), 'does not render exchange chip in card mode when exchange is absent').toBeNull();
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
  });

  describe('accessibility', () => {
    it('caret icon has aria-hidden in card mode', async () => {
      const { container } = await render(TickerQuoteComponent, {
        inputs: { quote: BASE_QUOTE, mode: 'card' },
      });

      const caret = requireElement(container.querySelector('i'), 'i');
      expect(caret.getAttribute('aria-hidden')).toBe('true');
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
