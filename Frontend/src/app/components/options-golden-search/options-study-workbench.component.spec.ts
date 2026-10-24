import { render, screen } from '@testing-library/angular';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';

import { fakeScopedHealth, healthItem, projection } from '../../shared/scoped-health/testing/scoped-health-fixtures';
import { OptionsGoldenSearchService } from './options-golden-search.service';
import { OptionsStudyWorkbenchComponent } from './options-study-workbench.component';
import { study } from './testing';

function service() {
  return { get: vi.fn(async (_id: string) => study()), command: vi.fn(async (_id: string, _request: unknown) => study()) };
}

async function workbench(fake = service(), studyId = 'study-1') {
  const view = await render(OptionsStudyWorkbenchComponent, { inputs: { studyId }, providers: [{ provide: OptionsGoldenSearchService, useValue: fake }] });
  return { view, fake };
}

describe('One golden search study', () => {
  it('tells the operator what the data plane says of the archived history a study replays', async () => {
    const health = fakeScopedHealth(
      projection(healthItem({ kind: 'option_archive', instrument: 'QQQ', severity: 'critical', explanation: 'QQQ: capture is failing.', link: { tab: 'option-archive', query: {}, fragment: 'option-archive-QQQ' } })),
    );

    await render(OptionsStudyWorkbenchComponent, { inputs: { studyId: 'study-1' }, providers: [{ provide: OptionsGoldenSearchService, useValue: service() }, health.provider] });

    expect(await screen.findByText(/QQQ: capture is failing/)).toBeTruthy();
    expect(health.read).toHaveBeenCalledWith({ operations: ['options_history', 'recovery'], instrument: null });
    expect(screen.getByRole('link', { name: 'Open Option archive' }).getAttribute('href')).toBe('/operations/option-archive#option-archive-QQQ');
    expect(await screen.findByRole('button', { name: 'Run research' })).toBeTruthy();
  });

  it('reopens a frozen final batch on the final step before its first result', async () => {
    const fake = service();
    fake.get.mockResolvedValue(study({ phase: 'final', status: 'interrupted', final_results: [], allowed_commands: ['resume', 'keep_original'] }));
    await workbench(fake);
    expect(await screen.findByRole('heading', { name: 'Final outcomes and approval' })).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Resume' })).toBeTruthy();
    expect(screen.queryByRole('button', { name: 'Freeze shortlist and open final test' })).toBeNull();
  });

  it('sends the durable revision and reuses the command intent after a transport failure', async () => {
    const fake = service();
    fake.command.mockRejectedValue(new Error('Connection lost'));
    await workbench(fake);
    const user = userEvent.setup();
    await user.click(await screen.findByRole('button', { name: 'Run research' }));
    await screen.findByText('The command did not complete. Refresh to read the latest durable state.');
    await user.click(screen.getByRole('button', { name: 'Run research' }));
    expect(fake.command).toHaveBeenCalledTimes(2);
    expect(fake.command.mock.calls[0]).toEqual(fake.command.mock.calls[1]);
    expect(fake.command).toHaveBeenCalledWith('study-1', expect.objectContaining({ command: 'run_research', expected_revision: 4, idempotency_key: expect.any(String) }));
  });

  it('shows nothing of the study it was on while it reads another, and drops a late answer for the first', async () => {
    const fake = service();
    let answerFirst: ((value: ReturnType<typeof study>) => void) | undefined;
    fake.get.mockImplementation((id: string) => id === 'study-1' ? new Promise((resolve) => { answerFirst = resolve; }) : Promise.resolve(study({ id: 'study-2', setup_name: 'Other setup' })));
    const { view } = await workbench(fake, 'study-1');

    await view.rerender({ inputs: { studyId: 'study-2' } });
    expect(await screen.findByRole('heading', { name: 'Other setup' })).toBeTruthy();
    answerFirst?.(study({ id: 'study-1', setup_name: 'First setup' }));
    await view.fixture.whenStable();

    expect(screen.queryByRole('heading', { name: 'First setup' })).toBeNull();
    expect(screen.getByRole('heading', { name: 'Other setup' })).toBeTruthy();
  });

  it('says a study could not be read and offers to read it again', async () => {
    const fake = service();
    fake.get.mockRejectedValueOnce(new Error('Store offline')).mockResolvedValue(study());
    await workbench(fake);

    expect(await screen.findByText('The study could not be loaded.')).toBeTruthy();
    await userEvent.setup().click(screen.getByRole('button', { name: 'Read the study again' }));
    expect(await screen.findByRole('button', { name: 'Run research' })).toBeTruthy();
    expect(screen.queryByText('The study could not be loaded.')).toBeNull();
  });
});
