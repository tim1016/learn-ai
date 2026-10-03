/** Every chart's guide buttons have somewhere to go (#2821): the served
 * Golden Search guide has a section under the chart's `{#anchor}`, where
 * "About this chart" opens the drawer, headed by the panel's title and
 * opening on the panel's question, and that section's "How to read it" list
 * has one step for each step "Walk me through it" lights up. The guide's
 * reading path lists the charts Review this study visits, in its order.
 *
 * Falsifiability: rename a section's `{#anchor}`, retitle a panel or reword
 * its question on one side only, add or drop a reading step on one side
 * only, or reorder the tour without the reading path, and this spec names
 * what drifted. */
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { describe, expect, it } from 'vitest';

import { CHART_GUIDES } from './golden-search-chart-guides';
import { REVIEW_TOUR } from './golden-search-review-tour';

const GUIDE = readFileSync(join(__dirname, '../../../../assets/docs/golden-search-guide.md'), 'utf8').split('\n');

interface GuideSection {
  readonly title: string;
  readonly question: string | null;
  readonly readingSteps: readonly string[];
}

/** The section headed `… {#anchor}`, up to the next heading of its level or higher; null when no heading carries the anchor. */
function section(anchor: string): GuideSection | null {
  const start = GUIDE.findIndex((line) => new RegExp(`^#+ .*\\{#${anchor}\\}\\s*$`).test(line));
  if (start < 0) return null;
  const level = GUIDE[start].indexOf(' ');
  const end = GUIDE.findIndex((line, i) => i > start && /^#+ /.test(line) && line.indexOf(' ') <= level);
  const body = GUIDE.slice(start + 1, end < 0 ? undefined : end);
  const asked = body.findIndex((line) => /^#+ The question it answers\s*$/.test(line));
  const question = asked < 0 ? null : (body.slice(asked + 1).find((line) => line.trim() !== '')?.trim() ?? null);
  const title = GUIDE[start].replace(/^#+ /, '').replace(/\s*\{#[^}]+\}\s*$/, '');
  const reading = body.findIndex((line) => /^#+ How to read it\s*$/.test(line));
  const readingSteps: string[] = [];
  for (const line of reading < 0 ? [] : body.slice(reading + 1)) {
    if (/^#+ /.test(line)) break;
    const step = /^\d+\. (.+)$/.exec(line);
    if (step !== null) readingSteps.push(step[1].trim());
  }
  return { title, question, readingSteps };
}

describe('Golden Search guide and chart walkthroughs', () => {
  for (const [id, guide] of Object.entries(CHART_GUIDES)) {
    it(`${id}: the guide has its section, with one reading step per walkthrough step`, () => {
      const found = section(id);

      expect(found, `the guide has no heading anchored {#${id}}`).not.toBeNull();
      expect(found?.title).toBe(guide.title);
      expect(found?.question).toBe(guide.question);
      expect(found?.readingSteps).toHaveLength(guide.steps.length);
    });
  }

  it('the reading path lists the charts Review this study visits, in its order', () => {
    const start = GUIDE.findIndex((line) => /^## A reading path \{#reading-path\}\s*$/.test(line));
    const end = GUIDE.findIndex((line, i) => i > start && /^## /.test(line));
    const anchors = GUIDE.slice(start + 1, end).flatMap((line) => /^\d+\. \[[^\]]+\]\(#([a-z-]+)\)/.exec(line)?.slice(1) ?? []);

    expect(start).toBeGreaterThanOrEqual(0);
    expect(anchors).toEqual(REVIEW_TOUR.map((stop) => stop.chart));
  });
});
