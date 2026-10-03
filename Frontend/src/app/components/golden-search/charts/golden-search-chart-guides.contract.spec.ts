/** Every chart's guide buttons have somewhere to go (#2821): the served
 * Golden Search guide has a section under the chart's `{#anchor}`, where
 * "About this chart" opens the drawer, and that section's "How to read it"
 * list has one step for each step "Walk me through it" lights up. The
 * wording is the guide's to edit; only the structure is held.
 *
 * Falsifiability: rename a section's `{#anchor}`, or add or drop a reading
 * step on one side only, and this spec names the chart that drifted. */
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { describe, expect, it } from 'vitest';

import { CHART_GUIDES } from './golden-search-chart-guides';

const GUIDE = readFileSync(join(__dirname, '../../../../assets/docs/golden-search-guide.md'), 'utf8').split('\n');

interface GuideSection {
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
  return { readingSteps };
}

describe('Golden Search guide and chart walkthroughs', () => {
  for (const [id, guide] of Object.entries(CHART_GUIDES)) {
    it(`${id}: the guide has its section, with one reading step per walkthrough step`, () => {
      const found = section(id);

      expect(found, `the guide has no heading anchored {#${id}}`).not.toBeNull();
      expect(found?.readingSteps).toHaveLength(guide.steps.length);
    });
  }
});
