import { inject } from '@angular/core';
import { Router, type RedirectFunction } from '@angular/router';

import { redirectKeepingState, redirectState } from '../shell/route-redirects';
import {
  GOLDEN_PARENT_STUDY_KEY,
  OPTIONS_CHOOSE_GOLDEN,
  OPTIONS_CHOOSE_KEY,
  OPTIONS_SETUPS_ROOT,
  optionsEvidenceRoute,
  optionsSetupRoute,
  optionsStudyRoute,
  type OptionsEvidenceKind,
} from './options-setups.address';

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
 * `/options-golden-search` ends at the Golden tab of the setup it named
 * (`?setup_name=`), with the study it forks (`?parent_study_id=`) and every
 * other key and the fragment going with it. A link that names no setup is not
 * given one: it opens the Options setups list, which asks which setup to
 * continue with (`?then=golden`) and carries the fork along.
 */
export const redirectOptionsGoldenSearch: RedirectFunction = (source) => {
  const name: unknown = source.queryParams['setup_name'];
  const router = inject(Router);
  if (typeof name === 'string' && name !== '') {
    return router.createUrlTree(optionsSetupRoute(name, 'golden'), redirectState({ drop: ['setup_name'] }, source));
  }
  return router.createUrlTree([OPTIONS_SETUPS_ROOT], redirectState({ set: () => ({ [OPTIONS_CHOOSE_KEY]: OPTIONS_CHOOSE_GOLDEN }) }, source));
};

/**
 * `/options-golden-search/:studyId` ends at that study, at the address a study
 * has by its own id. The study freezes what it started from, so no saved setup
 * has to be found for it, and none is guessed: the page says which saved
 * setups hold that content today. The two keys that only named a plan to start
 * (`setup_name`, `parent_study_id`) mean nothing to a study that exists, so
 * they are not carried to it.
 */
export const redirectOptionsGoldenStudy = redirectKeepingState((params) => optionsStudyRoute(String(params['studyId'] ?? '')), {
  drop: ['setup_name', GOLDEN_PARENT_STUDY_KEY],
});
