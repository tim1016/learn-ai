import { Injectable, signal } from '@angular/core';

import type { EvidenceTab } from '../golden-search-evidence-tabs.component';
import { STUDY_STEPS, stepProgress, type StudyStep } from '../golden-search-steps';
import type { StudyDetail } from '../golden-search.types';
import { CHART_GUIDES, type GoldenSearchChartId } from './golden-search-chart-guides';

/** One stop of the study review: a chart on a step, inside the evidence tab that holds it when it lives in one. */
export interface ReviewStop {
  readonly step: StudyStep;
  readonly chart: GoldenSearchChartId;
  readonly tab: EvidenceTab | null;
}

/** The key charts in research order (#2821); the guide's reading path lists the same charts in the same order. */
export const REVIEW_TOUR: readonly ReviewStop[] = [
  { step: 'plan', chart: 'window-map', tab: null },
  { step: 'plan', chart: 'data-coverage', tab: null },
  { step: 'search', chart: 'search-path', tab: null },
  { step: 'search', chart: 'knob-profiles', tab: null },
  { step: 'search', chart: 'eligibility-map', tab: null },
  { step: 'test', chart: 'fold-timeline', tab: null },
  { step: 'test', chart: 'train-test-sharpe', tab: null },
  { step: 'test', chart: 'parameter-drift', tab: null },
  { step: 'test', chart: 'fold-activity', tab: null },
  { step: 'compare', chart: 'equity-and-fall', tab: null },
  { step: 'compare', chart: 'neighbor-tornado', tab: null },
  { step: 'compare', chart: 'cost-stress', tab: null },
  { step: 'compare', chart: 'without-best', tab: 'months' },
  { step: 'decision', chart: 'final-checks', tab: null },
  { step: 'decision', chart: 'final-comparison', tab: null },
];

/** The stops a study has reached: Search's once it has a result (its path only for Zoom), the final test's once it has an outcome, and every other step's once it is no longer ahead. */
export function reviewStops(study: StudyDetail): ReviewStop[] {
  return REVIEW_TOUR.filter((stop) => {
    if (stop.step === 'search') return study.results.search !== null && (stop.chart !== 'search-path' || study.protocol.method === 'zoom');
    if (stop.step === 'decision') return (study.results.exam?.outcome ?? null) !== null;
    return stepProgress(stop.step, study.state) !== 'upcoming';
  });
}

/** What the review says at a stop: the step, the chart and the question to check there. */
export function reviewStepText(stop: ReviewStop): string {
  const step = STUDY_STEPS.find((candidate) => candidate.id === stop.step)?.label ?? stop.step;
  const guide = CHART_GUIDES[stop.chart];
  return `${step} · ${guide.title}. ${guide.question}`;
}

/**
 * The chart the study review is on (#2821), provided by the study page:
 * the panel that shows it marks itself and comes into view, and the
 * evidence tab that holds it opens.
 */
@Injectable()
export class GoldenSearchReviewTour {
  readonly stop = signal<ReviewStop | null>(null);
}
