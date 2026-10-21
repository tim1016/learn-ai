import { expect, test } from '@playwright/test';

import candidate from '../../../PythonDataService/tests/fixtures/options_evidence/candidate-development.json';
import session from '../../../PythonDataService/tests/fixtures/options_evidence/candidate-session.json';
import study from '../../../PythonDataService/tests/fixtures/options_evidence/study-summary.json';

test('inspects the blocked PUT candidate and one retained trade without replaying', async ({ page }) => {
  const reads: string[] = [];
  const commands: string[] = [];
  await page.route('**/api/**', async route => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (request.method() !== 'GET') commands.push(path);
    if (path.endsWith(`/studies/${study.id}`)) await route.fulfill({ json: study });
    else if (path.endsWith('/candidate-evidence')) await route.fulfill({ json: { items: [{ candidate_hash: candidate.candidate_hash, label: candidate.candidate.label, state: 'retained', verdict: candidate.candidate.verdict, net_profit_usd: candidate.candidate.net_profit_usd, is_original: true }], total: 1, offset: 0, limit: 50 } });
    else if (path.endsWith(`/candidate-evidence/${candidate.candidate_hash}`)) { reads.push(path); await route.fulfill({ json: candidate }); }
    else if (path.includes('/sessions/')) { reads.push(path); await route.fulfill({ json: session }); }
    else if (path === '/api/jobs') await route.fulfill({ json: [] });
    else await route.fulfill({ status: 503, json: { detail: 'Outside this retained fixture.' } });
  });
  await page.goto(`/options-golden-search/${study.id}`);
  const workbench = page.locator('app-options-golden-search-page');
  await workbench.getByRole('button', { name: 'Compare and final test', exact: true }).click();
  await expect(workbench.getByRole('heading', { name: 'All candidate outcomes' })).toBeVisible();
  expect(reads).toEqual([]);
  await workbench.getByRole('button', { name: 'Inspect evidence', exact: true }).click();
  const detail = workbench.locator('app-options-candidate-evidence');
  await expect(detail.getByText(/4 \/ 373 scheduled sessions replayed/)).toBeVisible();
  await expect(detail.getByText(/369 sessions uncomputed/)).toBeVisible();
  await expect(detail.getByText(/Missing held marks: 2/)).toBeVisible();
  await expect(detail.getByText('-339.60 USD', { exact: true })).toBeVisible();
  expect(reads).toHaveLength(1);
  await detail.getByText('Exact candidate settings and costs', { exact: true }).click();
  await expect(detail.getByText(/The entry is read/)).toBeVisible();
  await detail.getByText('Session, leg and attempt evidence', { exact: true }).click();
  const traded = detail.locator('details details > summary').filter({ hasText: 'Traded' }).first();
  await traded.click();
  await expect(detail.getByRole('cell', { name: session.attempts[0].contract, exact: true })).toBeVisible();
  expect(reads).toHaveLength(2);
  await page.screenshot({ path: 'test-results/options-candidate-retained-desktop.png', fullPage: true });
  await page.setViewportSize({ width: 390, height: 844 });
  expect(await workbench.evaluate(element => element.scrollWidth <= element.clientWidth)).toBe(true);
  await page.screenshot({ path: 'test-results/options-candidate-retained-narrow.png', fullPage: true });
  expect(commands).toEqual([]);
});
