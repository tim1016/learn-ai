import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { expect, test } from '@playwright/test';

import { RUN_WORKSPACE, STUDY_WORKSPACE } from '../../src/app/components/options-evidence/testing';

const fixture = (name: string) => JSON.parse(readFileSync(resolve(__dirname, `../../../PythonDataService/tests/fixtures/options_evidence/${name}.json`), 'utf8'));

test('opens source-76 performance, exact Golden lineage and frozen PUT search without commands', async ({ page }) => {
  const commands: string[] = [];
  const errors: string[] = [];
  page.on('pageerror', error => errors.push(error.message));
  await page.route('**/api/**', async route => {
    const path = new URL(route.request().url()).pathname;
    if (route.request().method() !== 'GET' && !path.endsWith('/setups/describe')) commands.push(path);
    if (path.includes('/workspace/run/76')) await route.fulfill({ json: RUN_WORKSPACE });
    else if (path.includes('/workspace/golden/')) await route.fulfill({ json: { ...RUN_WORKSPACE, subject_kind: 'golden', subject_id: RUN_WORKSPACE.approvals[0].golden_id } });
    else if (path.includes('/workspace/study/')) await route.fulfill({ json: STUDY_WORKSPACE });
    else if (path.endsWith('/runs/76')) await route.fulfill({ json: fixture('run76') });
    else if (path.endsWith('/setups/describe')) await route.fulfill({ json: fixture(route.request().postDataJSON().legs.length === 1 ? 'description-study' : 'description76') });
    else if (path === '/api/jobs') await route.fulfill({ json: [] });
    else await route.fulfill({ status: 503, json: { detail: 'Outside this retained evidence fixture.' } });
  });
  await page.goto('/options-evidence/run/76');
  const workspace = page.locator('app-options-evidence-page');
  await expect(workspace.getByRole('heading', { name: RUN_WORKSPACE.headline })).toBeVisible();
  await expect(workspace.locator('app-options-backtest-run-daily-chart')).toBeVisible();
  await page.screenshot({ path: 'test-results/options-evidence-performance-desktop.png', fullPage: true });
  await page.setViewportSize({ width: 390, height: 844 });
  expect(await workspace.evaluate(element => element.scrollWidth <= element.clientWidth)).toBe(true);
  await page.screenshot({ path: 'test-results/options-evidence-performance-narrow.png', fullPage: true });
  await workspace.getByRole('link', { name: 'Research lineage', exact: true }).click();
  await expect(workspace.getByRole('heading', { name: 'Approved from backtest' })).toBeVisible();
  await workspace.getByRole('link', { name: 'Open exact Golden version' }).click();
  await expect(page).toHaveURL(new RegExp(`/options-evidence/golden/${RUN_WORKSPACE.approvals[0].golden_id}`));
  await expect(workspace.getByText(/Execution proof: Ready/)).toBeVisible();
  expect(commands).toEqual([]);
  await page.goto(`/options-evidence/study/${STUDY_WORKSPACE.subject_id}`);
  await expect(workspace.getByRole('heading', { name: STUDY_WORKSPACE.headline })).toBeVisible();
  await expect(workspace.getByText(/4 observed \/ 373 scheduled sessions/)).toBeVisible();
  await workspace.getByText('Exact strategy settings and costs', { exact: true }).click();
  await expect(workspace.getByText(/20, 25, 30/)).toBeVisible();
  expect(await workspace.evaluate(element => element.scrollWidth <= element.clientWidth)).toBe(true);
  await page.screenshot({ path: 'test-results/options-evidence-study-narrow.png', fullPage: true });
  expect(commands).toEqual([]);
  expect(errors).toEqual([]);
});
