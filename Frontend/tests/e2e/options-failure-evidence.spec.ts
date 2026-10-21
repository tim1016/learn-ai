import { expect, test } from '@playwright/test';

import candidate from '../../../PythonDataService/tests/fixtures/options_evidence/candidate-development.json';
import failures from '../../../PythonDataService/tests/fixtures/options_evidence/failure-development.json';
import study from '../../../PythonDataService/tests/fixtures/options_evidence/study-summary.json';

test('opens retained missing-mark diagnosis and explicitly inspects one archived input', async ({ page }) => {
  const commands: string[] = [];
  const inspections: string[] = [];
  const errors: string[] = [];
  page.on('pageerror', error => errors.push(error.message));
  await page.route('**/api/**', async route => {
    const request = route.request();
    const url = new URL(request.url());
    const path = url.pathname;
    if (request.method() !== 'GET') commands.push(path);
    if (path.endsWith('/failures/inspect')) {
      inspections.push(request.url());
      await route.fulfill({ json: { study_id: failures.study_id, candidate_hash: failures.candidate_hash, evidence_identity: failures.evidence_identity, event_id: failures.items[0].id, code: 'no_eligible_trade_bar', retained_path: failures.items[0].retained_path, expected_input_sha256: failures.items[0].expected_input_sha256, snapshot_input_sha256: failures.items[0].expected_input_sha256, input_verified: true, contains_eligible_trade_bar: false, explanation: 'The verified retained file has no eligible trade bar for this minute. This establishes a sparse input, not its upstream cause; downloading again is not a promised repair.' } });
    } else if (path.endsWith('/failures')) await route.fulfill({ json: failures });
    else if (path.endsWith(`/studies/${study.id}`)) await route.fulfill({ json: study });
    else if (path.endsWith('/candidate-evidence')) await route.fulfill({ json: { items: [{ candidate_hash: candidate.candidate_hash, label: candidate.candidate.label, state: 'retained', verdict: candidate.candidate.verdict, net_profit_usd: candidate.candidate.net_profit_usd, is_original: true }], total: 1, offset: 0, limit: 50 } });
    else if (path.endsWith(`/candidate-evidence/${candidate.candidate_hash}`)) await route.fulfill({ json: candidate });
    else if (path === '/api/jobs') await route.fulfill({ json: [] });
    else await route.fulfill({ status: 503, json: { detail: 'Outside this retained fixture.' } });
  });
  await page.goto(`/options-golden-search/${study.id}`);
  const workbench = page.locator('app-options-golden-search-page');
  await workbench.getByRole('button', { name: 'Compare and final test', exact: true }).click();
  await workbench.getByRole('button', { name: 'Inspect evidence', exact: true }).click();
  await workbench.getByText('View failure diagnostics', { exact: true }).click();
  const diagnostics = workbench.locator('app-options-failure-evidence');
  await expect(diagnostics.getByText('597 / 599 required held marks present; 2 missing.')).toBeVisible();
  await expect(diagnostics.getByText('0 / 6 eligible forward-fold winners. Final test: Not Opened.')).toBeVisible();
  await expect(diagnostics.getByText(/Cause not established/)).toHaveCount(2);
  await expect(diagnostics.getByText(/Real closing fill:/)).toHaveCount(2);
  expect(inspections).toHaveLength(0);
  await diagnostics.getByRole('button', { name: 'Inspect retained input', exact: true }).first().click();
  await expect(diagnostics.getByText('No Eligible Trade Bar', { exact: true })).toBeVisible();
  await expect(diagnostics.getByText(/not its upstream cause/)).toBeVisible();
  expect(inspections).toHaveLength(1);
  expect(inspections[0]).toContain('at_ms=1730925480000');
  await diagnostics.screenshot({ path: 'test-results/options-failure-diagnostics-desktop.png' });
  await page.setViewportSize({ width: 390, height: 844 });
  expect(await workbench.evaluate(element => element.scrollWidth <= element.clientWidth)).toBe(true);
  await diagnostics.screenshot({ path: 'test-results/options-failure-diagnostics-narrow.png' });
  await diagnostics.getByRole('link', { name: 'Prepare a new linked plan' }).click();
  await expect(page).toHaveURL(new RegExp(`parent_study_id=${study.id}`));
  expect(commands).toEqual([]);
  expect(errors).toEqual([]);
});
