import { LANE_MODE_WORDING } from '../../../services/alpaca-live-verdict.service';
import type { BudgetDeployReceipt } from '../v2-panel/lib/broker-v2-panel.service';

/** Where one bot trades: Dry Run, or the lane's own broker world. */
export type DeployWorld = 'dry_run' | 'paper' | 'shadow' | 'live';

/** One wording per world (PRD #2560 "Visual system and copy"): the lanes'
 * own words, and Dry Run's simulated cash. */
export const DEPLOY_WORLD_WORDING: Readonly<Record<DeployWorld, string>> = {
  ...LANE_MODE_WORDING,
  dry_run: 'DRY RUN · simulated cash',
};

const WORLD_OF_BUDGET: Readonly<Record<BudgetDeployReceipt['world'], DeployWorld>> = {
  synthetic: 'dry_run',
  real_paper: 'paper',
  shadow: 'shadow',
  real_live: 'live',
};

/** The world a budget receipt or preview names, as Deploy words it. */
export function deployWorldOf(world: BudgetDeployReceipt['world']): DeployWorld {
  return WORLD_OF_BUDGET[world];
}
