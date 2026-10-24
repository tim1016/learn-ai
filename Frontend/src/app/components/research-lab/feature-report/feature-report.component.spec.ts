import { render, screen } from '@testing-library/angular';
import { describe, expect, it, vi } from 'vitest';

import { FEATURE_RESULT } from '../../../research/stored-study/testing/stored-study-fixtures';
import { toResearchResult, type FeatureResearchJobResultRaw } from '../feature-runner/feature-result.mapper';
import { FeatureReportComponent } from './feature-report.component';

// Canvas drawing is Chart.js's job and jsdom has no canvas: the report's own output is the DOM around the charts.
vi.mock('chart.js', () => ({
  Chart: Object.assign(
    vi.fn(function () {
      return { destroy: vi.fn() };
    }),
    { register: vi.fn() },
  ),
  registerables: [],
}));

function screenOf(name: string) {
  return { name, description: `${name} screen.`, passed: true, required_for_stage1: true, failure_reasons: [] };
}

/** The Stage verdict as the job writes it. `stage_info` is left out of a verdict stored before the stage was recorded. */
function verdictWire(stageInfo: unknown) {
  return {
    statistical_screen: screenOf('Statistical'),
    economic_screen: screenOf('Economic'),
    oos_screen: screenOf('Out of sample'),
    multiple_testing_screen: screenOf('Multiple testing'),
    regime_stability_screen: screenOf('Regime stability'),
    multiple_testing: { raw_nw_p_value: 0.04, holm_p_value: 0.08, n_family: 4, note: 'Holm-adjusted across the family.' },
    cost_viability: {
      gross_spread_bps_signed: 4,
      directional_spread_bps: 4,
      cost_assumption_one_way_bps: 2,
      cost_erasure_one_way_bps: 3,
      net_spread_bps_at_assumption: 0,
      viable_at_assumption: false,
      spec_direction: 'momentum',
      note: '',
    },
    ic_ci: {
      point: 0.01,
      se: 0.005,
      ci_lower: 0,
      ci_upper: 0.02,
      confidence_level: 0.95,
      n_eff_used: 58,
      valid: true,
      se_approximation_note: 'Normal approximation.',
    },
    direction_matches_spec: true,
    target_signed_appropriate: true,
    ...(stageInfo === undefined ? {} : { stage_info: stageInfo }),
    final_decision: 'Research candidate: confirm out of sample.',
  };
}

const STAGE_2 = {
  stage: 2,
  label: 'Research candidate',
  description: 'Worth a longer sample.',
  next_stage_label: 'Promotion candidate',
  advance_criteria: [],
};

async function renderReport(verdict: unknown) {
  const raw = { ...FEATURE_RESULT, validation_verdict: verdict } as unknown as FeatureResearchJobResultRaw;
  const view = await render(FeatureReportComponent, { inputs: { result: toResearchResult(raw) } });
  return view.container as HTMLElement;
}

const follows = (first: Element, second: Element) =>
  Boolean(first.compareDocumentPosition(second) & Node.DOCUMENT_POSITION_FOLLOWING);

describe('FeatureReportComponent', () => {
  it('leads with the Stage verdict the server issued, and sets the page’s own grades below it as computed here', async () => {
    const page = await renderReport(verdictWire(STAGE_2));

    const stage = screen.getByText('Stage 2 — Research candidate');
    const grades = page.querySelector('[data-testid="page-computed-grades"]');
    if (grades === null) throw new Error('no grade section');
    expect(follows(stage, grades), 'the grades come after the Stage verdict').toBe(true);
    expect(grades.textContent).toContain('Computed in this page, not by the server.');
  });

  it('says “not recorded” for a result with no Stage verdict, never a Stage 0 rejection', async () => {
    const page = await renderReport(null);

    expect(screen.getByText('Stage not recorded')).toBeTruthy();
    expect(page.querySelector('[data-testid="stage-verdict-missing"]')?.textContent).toContain(
      'Not recorded: this result carries no Stage verdict from the server.',
    );
    expect(page.textContent).not.toContain('Stage 0');
    expect(page.textContent).not.toContain('Rejected');
  });

  it('says “not recorded” for a verdict stored without its stage, and maps it without failing', async () => {
    const page = await renderReport(verdictWire(undefined));

    expect(screen.getByText('Stage not recorded')).toBeTruthy();
    expect(page.textContent).not.toContain('Stage 0');
    expect(page.querySelector('.verdict-block')?.classList.contains('stage-none')).toBe(true);
  });
});
