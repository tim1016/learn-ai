// Run with the existing Frontend dev dependencies; this exercises only the prototype.
import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { readFileSync, writeFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import { dirname, resolve } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const directory = dirname(fileURLToPath(import.meta.url));
const frontend = process.env.GOLDEN_PREVIEW_FRONTEND_ROOT || resolve(directory, '../../../Frontend');
const require = createRequire(resolve(frontend, 'package.json'));
const { chromium } = require('@playwright/test');
const axe = readFileSync(require.resolve('axe-core/axe.min.js'), 'utf8');
const browser = await chromium.launch({ headless: true });
const page = await browser.newPage({ viewport: { width: 1040, height: 1000 }, colorScheme: 'dark' });
const receipt = { scope: 'Illustrative prototype only; no strategy, qualification or broker execution', checks: [], errors: [] };
page.on('pageerror', error => receipt.errors.push(error.message));
await page.addInitScript(() => {
  class PreviewTweak {
    constructor(options) { window.previewTweakOptions = options; }
    addToggle() {}
    addSelect(state) { window.previewTweakState = state; }
  }
  Object.defineProperty(window, 'Tweak', { get: () => PreviewTweak, set: () => {}, configurable: true });
});
let frame;
async function reset() {
  await page.goto(pathToFileURL(resolve(directory, 'index.html')).href);
  await page.frameLocator('iframe').locator('#golden-research').waitFor();
  frame = page.frames().find(candidate => candidate.parentFrame());
  await frame.addScriptTag({ content: axe });
}
async function audit(view, width, theme) {
  const violations = await frame.evaluate(async () => (await window.axe.run(document.getElementById('golden-research'), {
    runOnly: { type: 'tag', values: ['wcag2a', 'wcag2aa', 'wcag21aa'] },
  })).violations.map(item => ({ id: item.id, targets: item.nodes.map(node => node.target) })));
  const overflow = await frame.evaluate(() => document.documentElement.scrollWidth > window.innerWidth);
  receipt.checks.push({ view, width, theme, overflow, violations });
  assert.equal(overflow, false, `${view}: overflow at ${width}`);
  assert.deepEqual(violations, [], `${view}: accessibility violations`);
}
async function openExam() {
  await frame.locator('[data-page=decision]').click();
  assert.equal(await frame.locator('[data-action=exam]').isDisabled(), true);
  await frame.locator('#gs-exam-ack').check();
  await frame.locator('[data-action=exam]').click();
}
async function prepareApproval() {
  assert.equal(await frame.locator('[data-action=approve]').isDisabled(), true);
  await frame.locator('#gs-note').fill('Accept this exact configuration after reviewing the stated limitations.');
  assert.equal(await frame.locator('[data-action=approve]').isDisabled(), true);
  await frame.locator('#gs-review-ack').check();
}
try {
  for (const theme of ['dark', 'light']) {
    await page.emulateMedia({ colorScheme: theme });
    for (const width of [360, 1040]) {
      await page.setViewportSize({ width, height: 1000 });
      await reset();
      for (const view of ['compare', 'plan', 'search', 'validate', 'decision']) {
        await frame.locator(`[data-page=${view}]`).click();
        await audit(view, width, theme);
      }
      await openExam();
      await prepareApproval();
      await audit('approval-review', width, theme);
      await frame.locator('[data-action=approve]').click();
      assert.match(await frame.locator('#gs-content').innerText(), /Golden configuration ready in Deploy/);
      await audit('golden-approved', width, theme);
      if (width === 1040 && theme === 'dark') await page.screenshot({ path: resolve(directory, 'approval.png'), fullPage: true });
      await frame.locator('[data-action=deploy]').click();
      await audit('deploy-paper', width, theme);
      await frame.locator('#gs-deploy-mode').selectOption('Live');
      assert.match(await frame.locator('#gs-content').innerText(), /Choose a matching Live account/);
      assert.match(await frame.locator('#gs-content').innerText(), /gs-preview-024-v2/);
      await audit('deploy-live', width, theme);
    }
  }
  await page.emulateMedia({ colorScheme: 'dark' });
  await page.setViewportSize({ width: 1040, height: 1000 });
  await reset();
  await frame.locator('[data-candidate=recent]').click();
  assert.match(await frame.locator('.gs-aside').innerText(), /extra return comes with a warning/);
  await frame.locator('[data-candidate=steady]').click();
  await frame.locator('[data-cell="5,15"]').click();
  assert.match(await frame.locator('#gs-cell-detail').innerText(), /2.4/);
  for (const view of ['equity', 'months', 'trades', 'map']) await frame.locator(`[data-view=${view}]`).click();
  await frame.locator('[data-page=plan]').click();
  await frame.locator('[data-method=grid]').click();
  assert.match(await frame.locator('#gs-budget').innerText(), /exceeds the 5,000/);
  await frame.locator('[data-edit=gap-max]').fill('0.55');
  assert.match(await frame.locator('#gs-budget').innerText(), /preflight required/);
  await frame.locator('[data-page=compare]').click();
  await frame.locator('[data-page=plan]').click();
  assert.equal(await frame.locator('[data-edit=gap-max]').inputValue(), '0.55');
  receipt.checks.push({ behavior: 'Candidate guidance, evidence tabs, cell inspection, Grid budget and retained draft edits', passed: true });

  for (const scenario of ['sparse', 'reused', 'failed_research', 'proof_failed']) {
    await reset();
    if (scenario === 'failed_research') await frame.locator('[data-candidate=recent]').click();
    else await frame.evaluate(value => { window.previewTweakState.scenario = value; window.previewTweakOptions.onChange(); }, scenario);
    await openExam();
    await prepareApproval();
    if (scenario !== 'proof_failed') {
      assert.equal(await frame.locator('[data-action=approve]').isDisabled(), true);
      await frame.locator('#gs-evidence-ack').check();
    }
    await frame.locator('[data-action=approve]').click();
    if (scenario === 'proof_failed') {
      assert.match(await frame.locator('#gs-content').innerText(), /Qualification failed · current default unchanged/);
      assert.equal(await frame.locator('[data-action=deploy]').count(), 0);
    } else {
      assert.match(await frame.locator('#gs-content').innerText(), /Research override recorded; the original warning remains/);
      await frame.locator('[data-action=deploy]').click();
      await frame.locator('#gs-deploy-mode').selectOption('Live');
      assert.match(await frame.locator('#gs-content').innerText(), /Weak evidence · override recorded/);
    }
    await audit(scenario, 1040, 'dark');
    receipt.checks.push({ behavior: scenario === 'proof_failed' ? 'Proof failure prevents publishing and Deploy handoff' : `${scenario}: separate acknowledgement required; warning survives approval and Live selection`, passed: true });
  }
  await reset();
  await openExam();
  await frame.locator('[data-page=compare]').click();
  for (const choice of ['steady', 'recent', 'current']) assert.equal(await frame.locator(`[data-candidate=${choice}]`).isDisabled(), true);
  await frame.locator('[data-action=retain]').click();
  assert.match(await frame.locator('#gs-content').innerText(), /Current settings retained/);
  receipt.checks.push({ behavior: 'Final-test candidate lock and keep-current outcome', passed: true });

  for (const width of [320, 360, 768, 1040]) {
    await page.setViewportSize({ width, height: 1000 });
    await reset();
    await audit('comparison-layout', width, 'dark');
    if (width === 360 || width === 1040) await page.screenshot({ path: resolve(directory, width === 360 ? 'mobile.png' : 'desktop.png'), fullPage: true });
  }
  assert.deepEqual(receipt.errors, []);
  receipt.sourceSha256 = createHash('sha256').update(readFileSync(resolve(directory, 'mockup.html'))).digest('hex');
  receipt.exportSha256 = createHash('sha256').update(readFileSync(resolve(directory, 'index.html'))).digest('hex');
  writeFileSync(resolve(directory, 'verification.json'), `${JSON.stringify(receipt, null, 2)}\n`);
  process.stdout.write(`${JSON.stringify({ checks: receipt.checks.length, errors: receipt.errors })}\n`);
} finally {
  await browser.close();
}
