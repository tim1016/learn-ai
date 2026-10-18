import type { PositionPreview } from './position-preview.service';

export function previewAnswer(change: Partial<PositionPreview> = {}): PositionPreview {
  return {
    source: 'historical', mode: 'position', at_ms: 1791552900000, spot_usd: '500',
    session_open_ms: 1791552600000, session_close_ms: 1791576000000,
    legs: [{ leg_id: 'call', symbol: 'SPY261009C00500000', side: 'buy', right: 'call', contracts: 1, strike_usd: '500', expiry_ms: 1791576000000, premium_usd: '2', price_at_ms: 1791552900000, price_source: 'Stored minute close' }],
    prices_usd: ['490', '500', '510'], curves: [{ leg_id: null, pnl_usd: ['-200', '-200', '800'] }, { leg_id: 'call', pnl_usd: ['-200', '-200', '800'] }],
    risk: { entry_debit_usd: '200', max_profit_usd: null, max_loss_usd: '200', breakevens_usd: ['502'] }, notes: [], events: [], ...change,
  };
}

export const quietPositionPreview = { preview: async () => previewAnswer({ legs: [], prices_usd: [], curves: [], risk: null }) };
