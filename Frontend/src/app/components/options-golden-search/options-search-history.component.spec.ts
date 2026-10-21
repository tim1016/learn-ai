import { ChangeDetectionStrategy, Component, signal } from '@angular/core';
import { fireEvent, render, screen, waitFor } from '@testing-library/angular';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';

import type { DateRange } from '../../shared/date-picker';
import { provideFakeDateWorld } from '../../shared/date-picker/testing/fake-trading-calendar';
import { OptionsGoldenSearchService } from './options-golden-search.service';
import type { HistoryGuidanceRequest } from './options-golden-search.types';
import { OptionsSearchHistoryComponent } from './options-search-history.component';
import { declarations, historyGuidance } from './testing';

@Component({
  imports: [OptionsSearchHistoryComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: '<app-options-search-history [request]="request()" [range]="range()" (rangeChanged)="range.set($event)" />',
})
class Host {
  readonly request = signal<HistoryGuidanceRequest>({ bounds: declarations().history_bounds, train_months: 6, test_months: 2, final_months: 6 });
  readonly range = signal<DateRange>({ from: '', to: '' });
}

async function history(fake = { historyGuidance: vi.fn(async (_request: HistoryGuidanceRequest) => historyGuidance()) }) {
  const view = await render(Host, { providers: [...provideFakeDateWorld(), { provide: OptionsGoldenSearchService, useValue: fake }] });
  return { ...view, fake, host: view.fixture.componentInstance };
}

describe('walk-forward history guidance', () => {
  it('autofills the server recommendation, preserves a custom edit and restores Max', async () => {
    const { host } = await history();
    await screen.findByRole('heading', { name: 'Maximum study interval selected' });
    expect(screen.getByRole('textbox', { name: 'Start date' })).toHaveProperty('value', '2024-01-02');
    expect(screen.getByRole('textbox', { name: 'End date' })).toHaveProperty('value', '2026-09-01');
    fireEvent.change(screen.getByRole('textbox', { name: 'Start date' }), { target: { value: '2024-02-01' } });
    host.request.update(value => ({ ...value, train_months: 8 }));
    await screen.findByRole('heading', { name: 'Suggested maximum study interval' });
    expect(screen.getByRole('textbox', { name: 'Start date' })).toHaveProperty('value', '2024-02-01');
    await userEvent.click(await screen.findByRole('button', { name: /^Max\./ }));
    await screen.findByRole('heading', { name: 'Maximum study interval selected' });
    expect(screen.getByRole('textbox', { name: 'Start date' })).toHaveProperty('value', '2024-01-02');
  });

  it('updates automatic dates when window lengths change without replacing an invalid or cleared date', async () => {
    const fake = { historyGuidance: vi.fn(async (request: HistoryGuidanceRequest) => request.test_months === 3 ? { ...historyGuidance(), start_ms: 1706850000000 } : historyGuidance()) };
    const { host } = await history(fake);
    await screen.findByRole('heading', { name: 'Maximum study interval selected' });
    host.request.update(value => ({ ...value, test_months: 3 }));
    await waitFor(() => expect(screen.getByRole('textbox', { name: 'Start date' })).toHaveProperty('value', '2024-02-02'));
    fireEvent.change(screen.getByRole('textbox', { name: 'Start date' }), { target: { value: '2024-02-03' } });
    expect(host.range().from).toBe('');
    expect(screen.getByRole('textbox', { name: 'Start date' })).toHaveProperty('value', '2024-02-03');
    fireEvent.change(screen.getByRole('textbox', { name: 'Start date' }), { target: { value: '' } });
    host.request.update(value => ({ ...value, final_months: 3 }));
    await waitFor(() => expect(fake.historyGuidance).toHaveBeenCalledTimes(3));
    expect(screen.getByRole('textbox', { name: 'Start date' })).toHaveProperty('value', '');
  });

  it('leaves dates empty when history cannot fit a complete study and offers retry after a failed read', async () => {
    const fake = { historyGuidance: vi.fn(async (_request: HistoryGuidanceRequest) => ({ ...historyGuidance(), start_ms: null, end_ms: null, final_start_ms: null, fold_count: 0, explanation: 'Retained history is too short.' })) };
    fake.historyGuidance.mockRejectedValueOnce(new Error('Date guidance unavailable'));
    await history(fake);
    await userEvent.click(await screen.findByRole('button', { name: 'Retry date guidance' }));
    await screen.findByText('Retained history is too short.');
    expect(screen.getByRole('textbox', { name: 'Start date' })).toHaveProperty('value', '');
    expect(screen.getByRole('textbox', { name: 'End date' })).toHaveProperty('value', '');
    expect(screen.getByRole('button', { name: /^Max,/ }).getAttribute('aria-disabled')).toBe('true');
  });
});
