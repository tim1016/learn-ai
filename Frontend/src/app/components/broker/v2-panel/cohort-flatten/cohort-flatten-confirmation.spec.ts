import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { describe, expect, it } from 'vitest';

import { COHORT_FLATTEN_COPY } from './cohort-flatten-confirmation';

/** The templates of every cohort-flatten component. */
const TEMPLATES = [
  'cohort-flatten-drawer.component.html',
  'cohort-flatten-group.component.html',
  'cohort-flatten-outcome.component.html',
];

/**
 * What is left of a template once everything that is not literal text is
 * removed: interpolations, tags (and so every attribute), comments and
 * control-flow syntax. Any remaining character is operator copy authored in
 * the template instead of the closed map.
 */
function literalText(template: string): string {
  return template
    .replace(/<!--[\s\S]*?-->/g, '')
    .replace(/\{\{[\s\S]*?\}\}/g, '')
    .replace(/<[^>]*>/g, '')
    .replace(/@(?:if|else|for|empty|switch|case|default)\b[^{]*\{/g, '')
    .replace(/\}/g, '')
    .replace(/\s+/g, '');
}

describe('the cohort-flatten operator-copy map (owner decision 2026-09-23)', () => {
  it('is a closed set of keys', () => {
    // Adding a key is a deliberate copy change and must update this list.
    expect(Object.keys(COHORT_FLATTEN_COPY).sort()).toEqual(
      [
        'eyebrow',
        'title',
        'intro',
        'closeLabel',
        'reading',
        'loadFailed',
        'empty',
        'selectCohort',
        'selectCohortLabel',
        'armedCount',
        'exposure',
        'capReached',
        'review',
        'inFlight',
        'confirmToken',
        'confirmHeading',
        'confirmMessage',
        'confirmConsequence',
        'confirmLabel',
        'retry',
        'requestUnknown',
        'requestRefused',
        'requestRefusedNext',
        'requestFallback',
        'outcomeRegion',
        'outcomeSummary',
        'outcomeLabel',
        'receipt',
        'accountBlockerHeading',
        'accountBlockerBody',
        'notAttempted',
        'staleRefusals',
      ].sort(),
    );
    expect(Object.keys(COHORT_FLATTEN_COPY.outcomeLabel).sort()).toEqual(
      ['applied', 'failed', 'refused', 'replayed', 'unknown'],
    );
  });

  it('keeps the per-bot flatten_stop confirmation token', () => {
    expect(COHORT_FLATTEN_COPY.confirmToken).toBe('FLATTEN');
  });

  it('interpolates only backend facts into the blast-radius copy', () => {
    const message = COHORT_FLATTEN_COPY.confirmMessage('PA1', 'Deployment Validation', [
      {
        strategy_instance_id: 'qqq-1',
        action_id: 'execute_safe_flatten',
        enabled: true,
        revision: 1,
        concurrency_token: 't',
        blocker_headline: null,
        exposure: { QQQ: 10 },
      },
      {
        strategy_instance_id: 'qqq-2',
        action_id: 'execute_safe_flatten',
        enabled: true,
        revision: 1,
        concurrency_token: 't',
        blocker_headline: null,
        exposure: {},
      },
    ]);

    expect(message).toBe(
      'This command targets 2 bots of Deployment Validation on account PA1. ' +
        'Attributed exposure: qqq-1 QQQ 10; qqq-2 none.',
    );
  });

  it.each(TEMPLATES)('%s renders no copy of its own', (file) => {
    const template = readFileSync(join(__dirname, file), 'utf8');

    expect(literalText(template)).toBe('');
    // Accessible names are copy too: only bound, never literal.
    expect(template).not.toMatch(/\saria-label="/);
  });
});
