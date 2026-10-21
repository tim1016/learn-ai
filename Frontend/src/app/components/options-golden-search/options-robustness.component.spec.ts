import { render, screen, waitFor } from '@testing-library/angular';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';

import source from '../../../../../PythonDataService/tests/fixtures/options_evidence/robustness-development.json';
import { OptionsRobustnessComponent } from './options-robustness.component';
import { OptionsRobustnessService, type CandidateRobustness, type NeighborPage } from './options-robustness.service';

const ROBUSTNESS = { ...source, scope: 'development' } satisfies CandidateRobustness;
const NEIGHBORS: NeighborPage = {
  study_id: source.study_id, protocol_hash: 'a'.repeat(64), selected_candidate_hash: source.candidate_hash,
  scope: 'development', start_ms: source.start_ms, end_ms: source.end_ms, objective: 'net_profit', offset: 0, limit: 50, total: 2,
  items: [{ ordinal: 0, dimension: 'PUT momentum', label: 'PUT momentum 20%, no stop', candidate_hash: 'b'.repeat(64), is_selected: false, state: 'retained', verdict: 'could_not_evaluate', net_profit_usd: '-346.90' },
    { ordinal: 1, dimension: 'PUT momentum', label: 'PUT momentum 30%, no stop', candidate_hash: 'c'.repeat(64), is_selected: false, state: 'untested', verdict: null, net_profit_usd: null }],
};

function providers(service: Partial<OptionsRobustnessService>) {
  return [{ provide: OptionsRobustnessService, useValue: service }];
}

describe('Retained Options robustness', () => {
  it('labels partial metrics and declared cost stress without inventing final or untested performance', async () => {
    const selected = vi.fn();
    await render(OptionsRobustnessComponent, {
      inputs: { studyId: source.study_id, digest: source.candidate_hash, canInspectNeighbors: true },
      on: { inspectNeighbor: selected },
      providers: providers({ read: async () => ROBUSTNESS, neighbors: async () => NEIGHBORS }),
    });
    expect(await screen.findByText(/4 \/ 373 scheduled sessions observed/)).toBeTruthy();
    expect(screen.getByText(/Partial diagnostic evidence/)).toBeTruthy();
    expect(screen.getByText(/No final outcome is exposed/)).toBeTruthy();
    expect(screen.getByText(/2× saved slippage/)).toBeTruthy();
    expect(screen.getByText(/Cash this path needed: \$339.95/)).toBeTruthy();
    expect(screen.getAllByText('Unavailable').length).toBeGreaterThan(0);
    const untested = screen.getByText('PUT momentum 30%, no stop').closest('tr');
    expect(untested?.textContent).toContain('Untested');
    expect(untested?.textContent).not.toContain('0.00');
    expect(screen.getAllByRole('button', { name: 'Inspect development evidence' })).toHaveLength(1);
    await userEvent.click(screen.getByRole('button', { name: 'Inspect development evidence' }));
    expect(selected).toHaveBeenCalledWith('b'.repeat(64));
  });

  it('discards an old candidate answer and resets neighbor pagination with the selected identity', async () => {
    let finish: ((value: CandidateRobustness) => void) | undefined;
    const old = new Promise<CandidateRobustness>(resolve => { finish = resolve; });
    const read = vi.fn().mockImplementation((_study: string, digest: string) => digest === source.candidate_hash ? old : Promise.resolve({ ...ROBUSTNESS, metrics: { ...ROBUSTNESS.metrics, net_profit_usd: '-99.00' } }));
    const neighbors = vi.fn().mockResolvedValue({ ...NEIGHBORS, total: 100 });
    const result = await render(OptionsRobustnessComponent, {
      inputs: { studyId: source.study_id, digest: source.candidate_hash }, providers: providers({ read, neighbors }),
    });
    await userEvent.click(await screen.findByRole('button', { name: 'Next neighbors' }));
    await waitFor(() => expect(neighbors).toHaveBeenLastCalledWith(source.study_id, source.candidate_hash, 'development', 50));
    await result.rerender({ inputs: { studyId: source.study_id, digest: 'd'.repeat(64) } });
    expect(await screen.findByText('-$99.00')).toBeTruthy();
    finish?.(ROBUSTNESS);
    await result.fixture.whenStable();
    expect(screen.getByText('Net diagnostic profit').parentElement?.textContent).toContain('-$99.00');
    expect(neighbors).toHaveBeenLastCalledWith(source.study_id, 'd'.repeat(64), 'development', 0);
  });

  it('retries unavailable artifacts and keeps neighbor failure separate from metric availability', async () => {
    const read = vi.fn().mockRejectedValueOnce(new Error('Missing')).mockResolvedValue(ROBUSTNESS);
    await render(OptionsRobustnessComponent, { inputs: { studyId: source.study_id, digest: source.candidate_hash },
      providers: providers({ read, neighbors: async () => { throw new Error('Unavailable'); } }),
    });
    await userEvent.click(await screen.findByRole('button', { name: 'Retry robustness' }));
    expect((await screen.findAllByText('-$339.60')).length).toBeGreaterThan(0);
    expect(screen.getByRole('button', { name: 'Retry neighbors' })).toBeTruthy();
  });
});
