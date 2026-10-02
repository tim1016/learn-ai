/** The guide and the walkthroughs tell the same story (#2821): every chart in
 * the registry has a section in the served Golden Search guide, under its
 * anchor and its panel title, and that section's "How to read it" list is
 * the chart's walkthrough, step for step and word for word.
 *
 * Falsifiability: reword a step in either file, drop a step, or rename a
 * section's `{#anchor}`, and this spec names the chart that drifted. */
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { describe, expect, it } from 'vitest';

import { CHART_GUIDES } from './golden-search-chart-guides';

const GUIDE = readFileSync(join(__dirname, '../../../../assets/docs/golden-search-guide.md'), 'utf8').split('\n');

interface GuideSection {
  readonly title: string;
  readonly readingSteps: readonly string[];
}

/** The section headed `… {#anchor}`, up to the next heading of its level or higher; null when no heading carries the anchor. */
function section(anchor: string): GuideSection | null {
  const start = GUIDE.findIndex((line) => new RegExp(`^#+ .*\\{#${anchor}\\}\\s*$`).test(line));
  if (start < 0) return null;
  const level = GUIDE[start].indexOf(' ');
  const end = GUIDE.findIndex((line, i) => i > start && /^#+ /.test(line) && line.indexOf(' ') <= level);
  const body = GUIDE.slice(start + 1, end < 0 ? undefined : end);
  const reading = body.findIndex((line) => /^#+ How to read it\s*$/.test(line));
  const readingSteps: string[] = [];
  for (const line of reading < 0 ? [] : body.slice(reading + 1)) {
    if (/^#+ /.test(line)) break;
    const step = /^\d+\. (.+)$/.exec(line);
    if (step !== null) readingSteps.push(step[1].trim());
  }
  return { title: GUIDE[start].replace(/^#+ /, '').replace(/\s*\{#[^}]+\}\s*$/, ''), readingSteps };
}

describe('Golden Search guide and chart walkthroughs', () => {
  for (const [id, guide] of Object.entries(CHART_GUIDES)) {
    it(`${id}: the guide's section matches the panel title and its reading steps are the walkthrough`, () => {
      const found = section(id);

      expect(found, `the guide has no heading anchored {#${id}}`).not.toBeNull();
      expect(found?.title).toBe(guide.title);
      expect(found?.readingSteps).toEqual(guide.steps.map((step) => step.text));
    });
  }
});
