import { expect, test } from '@playwright/test';

import { exceptionOptionsStudy, installOptionsSearchWorld, lockedOptionsStudy, OPTIONS_CANDIDATE_HASH, OPTIONS_SEARCH_ID, SAVED_OPTION_SETUP } from './support/options-search-world';

const STUDY_URL = `/options-setups/evidence/study/${OPTIONS_SEARCH_ID}`;

test('retains the original when no candidate qualifies', async ({ page }) => {
  const world = await installOptionsSearchWorld(page, lockedOptionsStudy({ phase: 'compare', status: 'completed', allowed_commands: ['keep_original'] }));
  // The old address lands straight on the study, at the address it has by its own id.
  await page.goto(`/options-golden-search/${OPTIONS_SEARCH_ID}`);
  await expect(page).toHaveURL(STUDY_URL);
  const workbench = page.locator('app-options-study-workbench');
  await expect(workbench.getByRole('heading', { name: 'Development and forward evidence' })).toBeVisible();
  await expect(workbench.getByRole('button', { name: 'Freeze shortlist and open final test' })).toBeDisabled();
  await workbench.getByRole('button', { name: 'Keep original' }).click();
  expect(world.commands[0]).toMatchObject({ command: 'keep_original', expected_revision: 1 });
  expect(world.jobStages).toEqual([]);
  expect(world.unexpected).toEqual([]);
});

test('cancels a partial final batch, reloads it, then explicitly resumes the frozen batch', async ({ page }) => {
  const initial = exceptionOptionsStudy();
  const world = await installOptionsSearchWorld(page, { ...initial, phase: 'final', status: 'running', allowed_commands: ['cancel'], approval_reviews: {} });
  await page.goto(STUDY_URL);
  const workbench = page.locator('app-options-study-workbench');
  await expect(workbench.getByRole('heading', { name: 'Final outcomes and approval' })).toBeVisible();
  await expect(workbench.getByRole('button', { name: 'Approve exact golden version' })).toBeDisabled();
  await workbench.getByRole('button', { name: 'Cancel', exact: true }).click();
  await page.reload();
  await expect(workbench.getByRole('button', { name: 'Resume', exact: true })).toBeVisible();
  expect(world.commands).toHaveLength(1);
  expect(world.jobStages).toEqual([]);
  await workbench.getByRole('button', { name: 'Resume', exact: true }).click();
  await expect.poll(() => world.jobStages).toContain('final-stage');
  expect(world.commands[1]).toMatchObject({ command: 'resume', expected_revision: 8 });
  expect(world.commands[1].candidate_hashes).toBeUndefined();
  expect(world.unexpected).toEqual([]);
});

test('locks a saved setup, resumes retained research, freezes final candidates and reviews exact approval', async ({ page }) => {
  const world = await installOptionsSearchWorld(page);
  const errors: string[] = [];
  page.on('pageerror', error => errors.push(error.message));
  // A golden search is started from the Golden tab of a saved setup, which the old address named by `?setup_name=`.
  await page.goto(`/options-golden-search?setup_name=${encodeURIComponent(SAVED_OPTION_SETUP.name)}`);
  await expect(page).toHaveURL(`/options-setups/${encodeURIComponent(SAVED_OPTION_SETUP.name)}/golden`);
  await page.getByRole('button', { name: 'Set up a golden search' }).click();
  const planner = page.locator('app-options-search-plan');
  await expect(planner.getByRole('heading', { name: 'Explore timing' })).toBeVisible();
  await planner.getByText('Additional settings to vary', { exact: true }).click();
  const stop = planner.getByRole('region', { name: 'Stop loss', exact: true });
  await stop.getByRole('button', { name: 'Explore', exact: true }).click();
  await stop.getByLabel('Minimum', { exact: true }).fill('20');
  await stop.getByLabel('Maximum', { exact: true }).fill('25');
  await stop.getByLabel('Step', { exact: true }).fill('5');
  await planner.getByLabel('Start date', { exact: true }).fill('2024-01-02');
  await planner.getByLabel('Start date', { exact: true }).press('Tab');
  await planner.getByLabel('End date', { exact: true }).fill('2026-09-18');
  await planner.getByLabel('End date', { exact: true }).press('Tab');
  await planner.locator('app-options-search-knobs').screenshot({ path: 'test-results/options-rule-editor.png' });
  await page.screenshot({ path: 'test-results/options-search-configuration-desktop.png', fullPage: true });
  await page.setViewportSize({ width: 390, height: 844 });
  expect(await planner.evaluate(element => element.scrollWidth <= element.clientWidth)).toBe(true);
  await page.screenshot({ path: 'test-results/options-search-configuration-mobile.png', fullPage: true });
  await page.setViewportSize({ width: 1440, height: 1100 });
  await planner.getByRole('button', { name: 'Check plan', exact: true }).click();
  await expect(planner.getByText('Dollar limit: 2000 USD')).toBeVisible();
  await planner.getByRole('button', { name: 'Start search', exact: true }).click();
  await expect(page).toHaveURL(STUDY_URL);
  expect(world.preflights[0]).toMatchObject({ expected_setup_hash: 'a'.repeat(64), knobs: [{ path: 'all_legs.stop_loss', values: [{ call: { kind: 'percent', value: '20.0000' }, put: { kind: 'percent', value: '20.0000' } }, { call: { kind: 'percent', value: '25.0000' }, put: { kind: 'percent', value: '25.0000' } }], mode: 'search', priority: 5 }], history: { start_ms: 1704171600000, end_ms: 1789790400000 } });
  expect(world.locks[0]).toMatchObject({ request: world.preflights[0], preflight_hash: 'b'.repeat(64) });
  await expect.poll(() => world.commands.length).toBe(1);
  await expect.poll(() => world.jobStages).toContain('research-stage');
  await page.reload();
  const study = page.locator('app-options-study-workbench');
  await expect(study.getByRole('heading', { name: 'Development and forward evidence' })).toBeVisible();
  expect(world.commands).toHaveLength(1);
  await study.getByRole('button', { name: 'Compare and final test', exact: true }).click();
  await expect(study.getByRole('checkbox', { name: 'Include Candidate one in final test' })).toBeChecked();
  await study.getByRole('button', { name: 'Freeze shortlist and open final test' }).click();
  await expect.poll(() => world.commands.length).toBe(2);
  expect(world.commands[1]).toMatchObject({ command: 'open_final', expected_revision: 3, candidate_hashes: [OPTIONS_CANDIDATE_HASH] });
  await expect.poll(() => world.jobStages).toContain('final-stage');
  await study.getByRole('button', { name: 'Refresh study', exact: true }).click();
  await expect(study.getByRole('heading', { name: 'Final outcomes and approval' })).toBeVisible();
  await study.getByRole('combobox', { name: 'Exact candidate to approve', exact: true }).selectOption(OPTIONS_CANDIDATE_HASH);
  await expect(study.getByRole('button', { name: 'Approve exact golden version' })).toBeEnabled();
  const selected = study.locator('app-options-search-candidate').filter({ has: page.getByRole('heading', { name: 'Candidate one', exact: true }) });
  await expect(selected.getByText('200.01234567890123456789 USD', { exact: true })).toBeVisible();
  expect(world.sessionReads).toHaveLength(0);
  await selected.getByText('Session, leg and attempt evidence', { exact: true }).click();
  expect(world.sessionReads).toHaveLength(0);
  await selected.locator('details details > summary').filter({ hasText: 'Traded' }).click();
  await expect(selected.getByRole('cell', { name: '0.65000000000000000001', exact: true })).toBeVisible();
  expect(world.sessionReads).toHaveLength(1);
  expect(world.sessionReads[0]).toContain('window=final');
  await page.screenshot({ path: 'test-results/options-golden-search-approval-desktop.png', fullPage: true });
  await page.setViewportSize({ width: 390, height: 844 });
  await page.screenshot({ path: 'test-results/options-golden-search-approval-mobile.png', fullPage: true });
  expect(await study.evaluate(element => element.scrollWidth <= element.clientWidth)).toBe(true);
  await expect(study.getByRole('button', { name: 'Approve exact golden version' })).toBeVisible();
  await study.getByLabel('Owner review comment').fill('Reviewed the exact final candidate and retained evidence.');
  await study.getByRole('button', { name: 'Approve exact golden version' }).click();
  await expect(study.getByText(/^Golden version /)).toContainText('ogs_browser_approved');
  expect(world.commands[2]).toMatchObject({ command: 'approve', candidate_hash: OPTIONS_CANDIDATE_HASH, expected_revision: 5 });
  expect(world.unexpected).toEqual([]);
  expect(errors).toEqual([]);
});

test('keeps failed performance rules visible and requires a deliberate owner exception', async ({ page }) => {
  const world = await installOptionsSearchWorld(page, exceptionOptionsStudy());
  await page.goto(STUDY_URL);
  const workbench = page.locator('app-options-study-workbench');
  await expect(workbench.getByRole('button', { name: 'Approve exact golden version' })).toBeDisabled();
  const exception = workbench.getByRole('button', { name: 'Approve owner exception' });
  await expect(exception).toBeDisabled();
  await expect(workbench.getByText('The final Sharpe floor was not met.', { exact: false }).last()).toBeVisible();
  await expect(workbench.getByText('This interval was used in another recorded Options study.', { exact: true })).toBeVisible();
  await workbench.getByLabel('Owner review comment').fill('   ');
  await expect(exception).toBeDisabled();
  await workbench.getByLabel('Owner review comment').fill('Accepted the failed Sharpe hurdle and previously used interval.');
  await expect(exception).toBeDisabled();
  const acknowledgements = workbench.getByRole('checkbox', { name: /^Acknowledge / });
  await expect(acknowledgements).toHaveCount(4);
  for (const checkbox of (await acknowledgements.all()).slice(0, 3)) await checkbox.check();
  await expect(exception).toBeDisabled();
  await acknowledgements.last().check();
  await page.screenshot({ path: 'test-results/options-golden-search-exception-acknowledged.png', fullPage: true });
  await exception.click();
  expect(world.commands[0]).toMatchObject({ command: 'approve_exception', expected_revision: 7, comment: 'Accepted the failed Sharpe hurdle and previously used interval.', waived_gate_ids: ['forward_0:activity', 'development:sharpe', 'final:sharpe', 'final:exposure'] });
  expect(world.unexpected).toEqual([]);
});

test('shows unverifiable results without allowing a technical exception', async ({ page }) => {
  const world = await installOptionsSearchWorld(page, exceptionOptionsStudy(true));
  await page.goto(STUDY_URL);
  const workbench = page.locator('app-options-study-workbench');
  await expect(workbench.locator('app-options-study-overview').getByRole('cell', { name: 'Could Not Evaluate' })).toBeVisible();
  await expect(workbench.getByRole('article').getByText('Could Not Evaluate', { exact: true })).toBeVisible();
  await workbench.getByLabel('Owner review comment').fill('This comment cannot override missing marks.');
  await expect(workbench.getByRole('button', { name: 'Approve owner exception' })).toBeDisabled();
  await expect(workbench.getByRole('button', { name: 'Approve exact golden version' })).toBeDisabled();
  await expect(workbench.getByRole('button', { name: 'Keep original' })).toBeEnabled();
  expect(world.commands).toHaveLength(0);
  expect(world.unexpected).toEqual([]);
});
