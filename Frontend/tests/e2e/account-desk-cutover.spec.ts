import AxeBuilder from '@axe-core/playwright';
import { expect, test, type Page } from '@playwright/test';

const ACCOUNT_ID = 'DU1234567';

async function installAccountsRoute(page: Page): Promise<void> {
  await page.route(`**/api/accounts/${ACCOUNT_ID}/triage`, (route) =>
    route.fulfill({
      json: {
        schema_version: 1,
        generated_at_ms: 1_780_000_000_000,
        account_id: ACCOUNT_ID,
        strategy_instance_id: null,
        summary_headline: 'Account recovery gates passing',
        summary_detail: `Account ${ACCOUNT_ID} has no blocking account triage rows.`,
        overall_gate_result: {
          gate_id: 'account.triage',
          status: 'pass',
          source: 'account_triage',
          operator_reason: `Account ${ACCOUNT_ID} has no blocking account triage rows.`,
          operator_next_step: 'ACCOUNT_TRIAGE_PASSING',
          evidence_at_ms: 1_780_000_000_000,
        },
        verdict: {
          state: 'CLEAN',
          headline: 'Account is clean',
          detail: 'The current reconciliation proof and account checks are passing.',
          primary_move: null,
          operator_attention_count: 0,
        },
        account_reconciliation_receipt: null,
        account_reconciliation_valid_until_ms: null,
        reconciliation_automation_policy: {
          schema_version: 1,
          account_id: ACCOUNT_ID,
          enabled: false,
          updated_at_ms: 1_780_000_000_000,
          updated_by: 'system.default',
        },
        account_observation: {
          state: 'ABSENT',
          reason_line: 'Account verification is not available yet.',
          observed_at_ms: null,
          valid_until_ms: null,
          history: [],
        },
        gate_rows: [],
        conditions: [],
        freeze_banner: null,
        clear_freeze_actionable: false,
        affected_bots: [],
        recovery_flatten_candidates: [],
        operator_blockers: [],
      },
    }),
  );
  await page.route('**/api/accounts', (route) =>
    route.fulfill({
      json: {
        schema_version: 1,
        rows: [
          {
            account_id: ACCOUNT_ID,
            broker: 'IBKR',
            effective_posture: 'UNKNOWN',
            service: { attachment: 'UNATTACHED', phase: null, generation: null },
            latest_verdict_summary: {
              state: 'NOT_PROVEN',
              headline: 'No live account observation is available.',
              generated_at_ms: 1_780_000_000_000,
            },
            last_verified_at_ms: null,
          },
        ],
      },
    }),
  );
}

test.describe('Account desk cutover', () => {
  test.use({ viewport: { width: 320, height: 720 } });

  test('redirects a legacy bookmark to its only account without accessibility or narrow-screen regressions', async ({ page }) => {
    await installAccountsRoute(page);

    await page.goto('/broker/account-monitor#account-reconciliation-action');

    await expect(page).toHaveURL(new RegExp(`/broker/accounts/${ACCOUNT_ID}(?:$|[?#])`));
    await expect(page.getByRole('button', { name: 'Operator', exact: true })).toHaveAttribute('aria-pressed', 'true');
    await expect(page.locator('#account-desk-recovery-controls')).toBeFocused();
    const fitsNarrowViewport = await page.locator('html').evaluate((element) =>
      element.scrollWidth <= element.clientWidth,
    );
    expect(fitsNarrowViewport).toBe(true);

    const results = await new AxeBuilder({ page })
      .withTags(['wcag2a', 'wcag2aa', 'wcag21aa'])
      .analyze();
    expect(results.violations).toEqual([]);
  });
});
