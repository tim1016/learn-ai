import { fireEvent, render, screen } from '@testing-library/angular';
import { expect, it } from 'vitest';

import { OptionsEvidenceChartComponent } from './options-evidence-chart.component';

it('inspects exact authored dollars without rounding a large balance', async () => {
  const points = [{ timestamp_ms: 1791294000000, value: '9007199254740993.01' }, { timestamp_ms: 1791294060000, value: '9007199254740993.02' }];
  const { container } = await render(OptionsEvidenceChartComponent, { inputs: { title: 'Equity', explanation: 'Recorded equity', points } });
  expect(container.querySelector('polyline')?.getAttribute('points')).toBe('0,148 1000,12');
  expect(screen.getByRole('status').textContent).toContain('9007199254740993.02 USD');
  fireEvent.input(screen.getByRole('slider'), { target: { value: '0' } });
  expect(screen.getByRole('status').textContent).toContain('9007199254740993.01 USD');
});

it('shows unavailable evidence rather than fabricating a zero path', async () => {
  const { container } = await render(OptionsEvidenceChartComponent, { inputs: { title: 'Drawdown', explanation: 'Recorded loss', points: [] } });
  expect(screen.getByText(/No verified risk path/)).toBeTruthy();
  expect(container.querySelector('polyline')).toBeNull();
  expect(screen.queryByRole('slider')).toBeNull();
});

it('labels the true dollar extrema even when nearby values share a pixel', async () => {
  const points = ['0', '999.999', '1000', '500'].map((value, index) => ({ timestamp_ms: 1791294000000 + index * 60000, value }));
  const view = await render(OptionsEvidenceChartComponent, { inputs: { title: 'Equity', explanation: 'Recorded equity', points } });
  expect([...view.container.querySelectorAll('.scale span')].map((label) => label.textContent)).toEqual(['1000 USD', '0 USD']);
});
