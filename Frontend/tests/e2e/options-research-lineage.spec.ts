import { expect, test } from '@playwright/test';

import lineage from '../../../PythonDataService/tests/fixtures/options_evidence/lineage-read.json';
import trials from '../../../PythonDataService/tests/fixtures/options_evidence/lineage-trials.json';
import description from '../../../PythonDataService/tests/fixtures/options_evidence/description-study.json';
import { STUDY_WORKSPACE } from '../../src/app/components/options-evidence/testing';

test('explains the actual frozen PUT procedure without opening final or loading unrelated traces', async ({ page }) => {
  const commands: string[] = [];
  const reads: string[] = [];
  const errors: string[] = [];
  page.on('pageerror', error => errors.push(error.message));
  await page.route('**/api/**', async route => {
    const path = new URL(route.request().url()).pathname;
    reads.push(path);
    if (route.request().method() !== 'GET' && !path.endsWith('/setups/describe')) commands.push(path);
    if (path.endsWith(`/workspace/study/${lineage.study_id}`)) await route.fulfill({ json: STUDY_WORKSPACE });
    else if (path.endsWith('/lineage/trials')) await route.fulfill({ json: trials });
    else if (path.endsWith('/lineage')) await route.fulfill({ json: lineage });
    else if (path.endsWith('/setups/describe')) await route.fulfill({ json: description });
    else if (path === '/api/jobs') await route.fulfill({ json: [] });
    else await route.fulfill({ status: 503, json: { detail: 'Outside the retained lineage fixture.' } });
  });
  await page.goto(`/options-setups/evidence/study/${lineage.study_id}?view=lineage`);
  const workspace = page.locator('app-options-evidence-page');
  const procedure = workspace.locator('app-options-research-lineage');
  await expect(procedure.getByRole('heading', { name: 'Why these parameters?' })).toBeVisible();
  await expect(procedure.getByText('27 / 27', { exact: true })).toBeVisible();
  await expect(procedure.getByText('No eligible training winner', { exact: true })).toHaveCount(6);
  await expect(procedure.getByText('The final test was not opened. No final outcomes are exposed by this view.')).toBeVisible();
  expect(reads.some(path => path.endsWith('/lineage/trials'))).toBe(false);
  await procedure.getByText('Inspect retained trial ledger', { exact: true }).click();
  await expect(procedure.locator('app-options-research-trials').getByText('Cache Hit', { exact: true })).toHaveCount(7);
  await page.screenshot({ path: 'test-results/options-research-lineage-desktop.png', fullPage: true });
  await page.setViewportSize({ width: 390, height: 844 });
  expect(await workspace.evaluate(element => element.scrollWidth <= element.clientWidth)).toBe(true);
  await page.screenshot({ path: 'test-results/options-research-lineage-narrow.png', fullPage: true });
  expect(reads.filter(path => path.includes('candidate-evidence') || path.includes('/final'))).toEqual([]);
  expect(commands).toEqual([]);
  expect(errors).toEqual([]);
});
