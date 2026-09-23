import {
  parseTemplate,
  TmplAstRecursiveVisitor,
  type TmplAstText,
  type TmplAstTextAttribute,
  tmplAstVisitAll,
} from '@angular/compiler';
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
 * Literal copy a template authors itself, found with Angular's own template
 * parser rather than by pattern-stripping markup: every non-blank static text
 * node (inside any element or control-flow block) and every literal
 * `aria-label`. Interpolations, bindings and comments are not literal text.
 * Any finding is operator copy authored in the template instead of the
 * closed map.
 */
class LiteralCopyCollector extends TmplAstRecursiveVisitor {
  readonly found: string[] = [];

  override visitText(text: TmplAstText): void {
    if (text.value.trim() !== '') {
      this.found.push(text.value.trim());
    }
  }

  override visitTextAttribute(attribute: TmplAstTextAttribute): void {
    // Accessible names are copy too: only bound, never literal.
    if (attribute.name === 'aria-label') {
      this.found.push(`aria-label="${attribute.value}"`);
    }
  }
}

function literalCopy(template: string, file: string): string[] {
  const parsed = parseTemplate(template, file, {});
  // A template that fails to parse would otherwise pass vacuously.
  expect(parsed.errors ?? []).toEqual([]);
  const collector = new LiteralCopyCollector();
  tmplAstVisitAll(collector, parsed.nodes);
  return collector.found;
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

    expect(literalCopy(template, file)).toEqual([]);
  });

  it('finds literal text and literal accessible names wherever a template hides them', () => {
    // Guards the collector itself, so an empty finding above means something.
    const template =
      '<!-- a comment is not copy -->' +
      '<p [attr.aria-label]="bound">{{ interpolated }}</p>' +
      '@if (shown) { <span>Receipt</span> } @else { Nothing }' +
      '<button aria-label="Close">x</button>';

    expect(literalCopy(template, 'probe.html')).toEqual([
      'Receipt',
      'Nothing',
      'aria-label="Close"',
      'x',
    ]);
  });
});
