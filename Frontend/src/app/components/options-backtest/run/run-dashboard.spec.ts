import { fireEvent, render, screen, waitFor, within } from '@testing-library/angular';
import { describe, expect, it, vi } from 'vitest';

import { RunDetailService } from '../run-detail/run-detail.service';
import { fakeRunDetail, inertRunDetail } from '../run-detail/testing/fake-run-detail';
import { dashboard } from '../testing/dashboard-fixture';
import { figures, runDetail } from '../testing/run-fixtures';
import { RunResultComponent } from './run-result.component';

describe('the completed-run Performance desk', () => {
  it('puts authored annualized Sharpe and session means in front, with calculations revealed on click', async () => {
    await render(RunResultComponent, { inputs: { run: runDetail({ figures: figures({ dashboard: dashboard() }) }) }, providers: [{ provide: RunDetailService, useValue: inertRunDetail() }] });
    const headline = screen.getByRole('region', { name: 'Headline figures' });
    expect(within(headline).getByText('1.23')).toBeTruthy();
    expect(screen.getByText('$250.25')).toBeTruthy();
    expect(within(headline).getByText('-$62.40')).toBeTruthy();
    expect(screen.queryByRole('complementary', { name: 'Selected statistic' })).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: 'Annualized Sharpe' }));
    const detail = screen.getByRole('complementary', { name: 'Selected statistic' });
    expect(within(detail).getByText(/matching Options Golden Search/)).toBeTruthy();
    expect(within(detail).getByText('251')).toBeTruthy();
    expect(within(detail).getByText('5')).toBeTruthy();
  });

  it('keeps material limitations visible while the evidence and trade requests stay closed', async () => {
    const service = fakeRunDetail(vi);
    const { container } = await render(RunResultComponent, {
      inputs: { run: runDetail({ figures: figures({ dashboard: dashboard() }) }) },
      providers: [{ provide: RunDetailService, useValue: service }],
    });
    expect(screen.getByRole('complementary', { name: 'Key limitations' }).textContent).toContain('2 entries refused for cash');
    expect(container.querySelector('[data-evidence="capital"]')?.hasAttribute('open')).toBe(false);
    expect(service.trades).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: '2 entries refused for cash' }));
    expect(container.querySelector('[data-evidence="capital"]')?.hasAttribute('open')).toBe(true);
  });

  it('shows a reason for unavailable risk metrics without inventing zero statistics', async () => {
    await render(RunResultComponent, { inputs: { run: runDetail({ figures: figures({ dashboard: dashboard({ annualized_sharpe: null, annualized_sortino: null, risk_unavailable_reason: 'A session ended with a marked, unclosed position.' }) }) }) }, providers: [{ provide: RunDetailService, useValue: inertRunDetail() }] });
    expect(screen.getByRole('button', { name: 'A session ended with a marked, unclosed position.' })).toBeTruthy();
    const button = screen.getByRole('button', { name: 'Annualized Sharpe' });
    expect(button.closest('.tile')?.textContent).toContain('Unavailable');
    fireEvent.click(button);
    expect(screen.getByRole('complementary', { name: 'Selected statistic' }).textContent).toContain('unclosed position');
  });

  it('keeps saved-path recovery visible when only calendar annualization is unavailable', async () => {
    const reason = 'The saved run used a different trading calendar; annualized risk statistics are unavailable.';
    await render(RunResultComponent, {
      inputs: { run: runDetail({ figures: figures({ dashboard: dashboard({ annualized_sharpe: null, display_annualized_sharpe: null, annualization_sessions: null, annualization_unavailable_reason: reason }) }) }) },
      providers: [{ provide: RunDetailService, useValue: inertRunDetail() }],
    });
    expect(screen.getByRole('button', { name: reason })).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Drawdown recovery' }).closest('.tile')?.textContent).toContain('3 sessions');
    fireEvent.click(screen.getByRole('button', { name: 'Annualized Sharpe' }));
    expect(screen.getByRole('complementary', { name: 'Selected statistic' }).textContent).toContain('different trading calendar');
  });

  it('loads the leg ledger only when its disclosure is opened', async () => {
    const service = fakeRunDetail(vi);
    await render(RunResultComponent, { inputs: { run: runDetail() }, providers: [{ provide: RunDetailService, useValue: service }] });
    expect(service.trades).not.toHaveBeenCalled();
    fireEvent.click(screen.getByText('Leg trades & session records'));
    await waitFor(() => expect(service.trades).toHaveBeenCalledTimes(1));
    fireEvent.click(screen.getByText('Leg trades & session records'));
    fireEvent.click(screen.getByText('Leg trades & session records'));
    await waitFor(() => expect(screen.getByRole('region', { name: 'Trades' })).toBeTruthy());
    expect(service.trades).toHaveBeenCalledTimes(1);
  });
});
