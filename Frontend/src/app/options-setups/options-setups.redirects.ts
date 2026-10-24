import { inject } from '@angular/core';
import { Router, type RedirectFunction } from '@angular/router';

import { redirectKeepingState, redirectState } from '../shell/route-redirects';
import { OPTIONS_SETUPS_ROOT, optionsAnalysisRoute, optionsEvidenceRoute, optionsSetupRoute, type OptionsEvidenceKind } from './options-setups.address';

/**
 * `/options-backtest` ends at the Options setups list; a link that carried the
 * setup it opened (`?setup_name=`) ends at that setup's Define tab instead, which
 * is where the old page put it. Either way every other query key and the
 * fragment go with it, in the one navigation.
 */
export const redirectOptionsBacktest: RedirectFunction = (source) => {
  const name: unknown = source.queryParams['setup_name'];
  const commands = typeof name === 'string' && name !== '' ? optionsSetupRoute(name, 'define') : [OPTIONS_SETUPS_ROOT];
  return inject(Router).createUrlTree(commands, redirectState({ drop: ['setup_name'] }, source));
};

/**
 * `/options-evidence/:kind/:subjectId` ends at the same evidence in the
 * Options setups place. The id is the evidence's own, so no saved name is
 * needed and nothing is looked up; the view and comparison the link asked for
 * go with it.
 */
export const redirectOptionsEvidence = redirectKeepingState((params) =>
  optionsEvidenceRoute(String(params['kind'] ?? '') as OptionsEvidenceKind, String(params['subjectId'] ?? '')),
);

/**
 * `/options-lab/strategy-builder` and `/options-lab/strategy-finder` end at the
 * same views in the Options setups place (#3516). Neither is a setup, so no
 * saved name is looked up or guessed; query and fragment go with the link.
 */
export const redirectOptionsStrategyBuilder = redirectKeepingState(optionsAnalysisRoute('builder'));
export const redirectOptionsStrategyFinder = redirectKeepingState(optionsAnalysisRoute('finder'));
