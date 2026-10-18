import { fireEvent, render, screen, waitFor } from '@testing-library/angular';
import { describe, expect, it, vi } from 'vitest';

import { provideFakeDateWorld } from '../../../shared/date-picker/testing/fake-trading-calendar';
import { exampleBody } from '../testing/fixtures';
import { PositionPreviewComponent } from './position-preview.component';
import { PositionPreviewService, type PositionPreview } from './position-preview.service';
import { previewAnswer } from './testing';

async function page(preview = vi.fn(async () => previewAnswer())) {
  const view = await render(PositionPreviewComponent, { inputs: { setup: exampleBody(), startingDate: '2026-10-09', debounceMs: 0 }, providers: [...provideFakeDateWorld(), { provide: PositionPreviewService, useValue: { preview } }] });
  return { view, preview };
}

describe('the live position preview', () => {
  it('reprices edited legs and keeps the chart and risk receipts from the same answer', async () => {
    const { view, preview } = await page();
    await screen.findByText('Underlying $500.00');
    const before = view.container.querySelector('polyline')?.getAttribute('points');
    const setup = exampleBody();
    const changed = { ...setup, legs: setup.legs.map(leg => ({ ...leg, right: 'put' as const, contracts: 2 })) };
    preview.mockResolvedValue(previewAnswer({ curves: [{ leg_id: null, pnl_usd: ['800', '-400', '-400'] }], risk: { entry_debit_usd: '400', max_profit_usd: '99600', max_loss_usd: '400', breakevens_usd: ['498'] } }));
    view.fixture.componentRef.setInput('setup', changed);
    await waitFor(() => expect(preview).toHaveBeenLastCalledWith(expect.objectContaining({ setup: changed })));
    await screen.findByText('$99,600.00');
    expect(view.container.querySelector('polyline')?.getAttribute('points')).not.toBe(before);
    expect(screen.getByText('$498.00')).toBeTruthy();
  });

  it('removes stale curves immediately and ignores a late earlier response', async () => {
    let release: (value: PositionPreview) => void = () => undefined;
    const preview = vi.fn().mockImplementationOnce(() => new Promise<PositionPreview>(resolve => { release = resolve; })).mockResolvedValue(previewAnswer({ spot_usd: '510' }));
    const { view } = await page(preview);
    await waitFor(() => expect(preview).toHaveBeenCalledTimes(1));
    view.fixture.componentRef.setInput('setup', { ...exampleBody(), starting_cash_usd: '20000' });
    await screen.findByText('Underlying $510.00');
    release(previewAnswer({ spot_usd: '999' }));
    await new Promise(resolve => setTimeout(resolve, 0));
    expect(screen.queryByText('Underlying $999.00')).toBeNull();
    view.fixture.componentRef.setInput('setup', null);
    await waitFor(() => expect(view.container.querySelector('svg')).toBeNull());
  });

  it('switches current market without a historical date and sends actual replay instants', async () => {
    const { view, preview } = await page();
    await waitFor(() => expect(preview).toHaveBeenCalled());
    fireEvent.click(screen.getByRole('button', { name: 'Session replay' }));
    await waitFor(() => expect(preview).toHaveBeenLastCalledWith(expect.objectContaining({ mode: 'replay' })));
    fireEvent.click(screen.getByRole('button', { name: 'Current market' }));
    await waitFor(() => expect(preview).toHaveBeenLastCalledWith(expect.objectContaining({ source: 'current', mode: 'position', trading_date_ms: null })));
    expect(screen.queryByRole('button', { name: 'Session replay' })).toBeNull();
    view.fixture.destroy();
  });
});

it('keeps replay navigation when a minute has no price and recovers at the next minute', async () => {
  const preview = vi.fn(async () => previewAnswer({ mode: 'replay' }));
  await page(preview);
  fireEvent.click(screen.getByRole('button', { name: 'Session replay' }));
  const slider = await screen.findByRole('slider', { name: 'Replay minute' });
  expect(slider.getAttribute('min')).toBe('1791552660000');
  preview.mockRejectedValueOnce(new Error('No bar at that minute'));
  fireEvent.change(slider, { target: { value: '1791552960000' } });
  await screen.findByText('The position preview could not be read. Try again.');
  const retained = screen.getByRole('slider', { name: 'Replay minute' });
  fireEvent.change(retained, { target: { value: '1791553020000' } });
  await screen.findByText('Underlying $500.00');
  expect(preview).toHaveBeenLastCalledWith(expect.objectContaining({ replay_at_ms: 1791553020000 }));
});
