import { render, screen, waitFor } from '@testing-library/angular';
import { describe, expect, it, vi } from 'vitest';

import type { BrokerPortfolioHistory } from '../../../api/alpaca.types';
import { AlpacaPortfolioHistoryChartComponent } from './alpaca-portfolio-history-chart.component';

const { chart, createChart, series } = vi.hoisted(() => {
  const mockedSeries = { setData: vi.fn() };
  const mockedChart = {
    addSeries: vi.fn().mockReturnValue(mockedSeries),
    applyOptions: vi.fn(),
    remove: vi.fn(),
    timeScale: vi.fn().mockReturnValue({ fitContent: vi.fn() }),
  };
  return {
    chart: mockedChart,
    createChart: vi.fn().mockReturnValue(mockedChart),
    series: mockedSeries,
  };
});

vi.mock('lightweight-charts', () => ({
  createChart,
  LineSeries: 'LineSeries',
}));

const history: BrokerPortfolioHistory = {
  timestamps: [1_700_000_000_000, 1_700_086_400_000],
  equity: [10_000, 10_125],
  profit_loss: [0, 125],
  base_value: 10_000,
  timeframe: '1D',
};

describe('AlpacaPortfolioHistoryChartComponent', () => {
  it('renders the broker-supplied equity points and recreates its chart when the view returns', async () => {
    chart.addSeries.mockClear();
    chart.remove.mockClear();
    series.setData.mockClear();
    createChart.mockClear();
    const view = await render(AlpacaPortfolioHistoryChartComponent, {
      componentInputs: { history, scopeLabel: '30D', unavailable: false },
    });

    expect(await screen.findByRole('img', { name: '30D broker equity curve' })).toBeTruthy();
    await waitFor(() =>
      expect(series.setData).toHaveBeenLastCalledWith([
        { time: 1_700_000_000, value: 10_000 },
        { time: 1_700_086_400, value: 10_125 },
      ]),
    );

    view.fixture.componentRef.setInput('history', { ...history, timestamps: [], equity: [], profit_loss: [] });
    view.fixture.detectChanges();
    expect(screen.queryByRole('img', { name: '30D broker equity curve' })).toBeNull();
    expect(chart.remove).toHaveBeenCalledTimes(1);

    view.fixture.componentRef.setInput('history', history);
    view.fixture.detectChanges();
    expect(await screen.findByRole('img', { name: '30D broker equity curve' })).toBeTruthy();
    expect(createChart).toHaveBeenCalledTimes(2);
  });
});
