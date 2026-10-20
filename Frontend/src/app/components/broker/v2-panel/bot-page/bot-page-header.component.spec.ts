/** #3164: the run summary's feed line says what the market-data feed is doing, not that the bot runs. */
import { render, screen } from '@testing-library/angular';
import userEvent from '@testing-library/user-event';
import { provideRouter } from '@angular/router';
import { describe, expect, it } from 'vitest';

import { fakeBotPanelView } from '../../../../testing/bot-panel-fixtures';
import { formatTimestampDisplay } from '../../../../shared/timestamp/timestamp-display';
import type { BotPanelView, MarketPulseView } from '../lib/broker-v2-panel.types';
import { BotPageHeaderComponent } from './bot-page-header.component';

const PANEL_ASSEMBLED_AT_MS = 1_700_000_300_000;
const LAST_BAR_AT_MS = 1_700_000_000_000;

function clock(ms: number): string {
  return formatTimestampDisplay(ms, { mode: 'local', granularity: 'time' });
}

function pulse(overrides: Partial<MarketPulseView>): MarketPulseView {
  return { ...fakeBotPanelView().market_pulse, latest_bar_at_ms: LAST_BAR_AT_MS, ...overrides };
}

/** The feed line the operator reads after opening the status button. */
async function feedLine(panel: BotPanelView): Promise<string> {
  await render(BotPageHeaderComponent, {
    inputs: { panel, backRoute: ['/'], backLabel: 'Home' },
    providers: [provideRouter([])],
  });
  await userEvent.setup().click(screen.getByRole('button', { name: /Running|Finished|Ended holding/ }));
  const line = screen.getByText(/^IBKR market data/);
  return (line.textContent ?? '').replace(/\s+/g, ' ').trim();
}

describe('BotPageHeaderComponent feed line (#3164)', () => {
  it.each([
    ['LIVE', 'live'],
    ['STALE', 'stale'],
    ['IDLE', 'idle'],
  ] as const)('reads a %s feed as "%s" (only a LIVE feed says live), and dates it by its last bar', async (feedState, word) => {
    const line = await feedLine(fakeBotPanelView({
      updated_at_ms: PANEL_ASSEMBLED_AT_MS,
      market_pulse: pulse({ feed_state: feedState }),
    }));

    expect(line).toBe(`IBKR market data ${word} · updated ${clock(LAST_BAR_AT_MS)}`);
  });

  it('says missing, with no time, when the feed has delivered nothing', async () => {
    const line = await feedLine(fakeBotPanelView({
      updated_at_ms: PANEL_ASSEMBLED_AT_MS,
      market_pulse: pulse({ feed_state: 'MISSING', latest_bar_at_ms: null, age_ms: null }),
    }));

    expect(line).toBe('IBKR market data missing');
  });

  it('does not say live while the feed of the asset the bot trades is stale (#2912)', async () => {
    const line = await feedLine(fakeBotPanelView({
      updated_at_ms: PANEL_ASSEMBLED_AT_MS,
      market_pulse: pulse({ feed_state: 'LIVE' }),
      traded_market_pulse: pulse({ feed_state: 'STALE', latest_bar_at_ms: LAST_BAR_AT_MS - 600_000 }),
    }));

    expect(line).toBe(`IBKR market data stale · updated ${clock(LAST_BAR_AT_MS - 600_000)}`);
  });
});
