import { LANE_MODE_WORDING } from '../../../services/alpaca-live-verdict.service';
import type { BudgetDeployReceipt } from '../v2-panel/lib/broker-v2-panel.service';

/** Where one bot trades: Dry Run, or the lane's own broker world. */
export type DeployWorld = 'dry_run' | 'paper' | 'shadow' | 'live';

interface DeployWorldWords {
  /** The world's name: "Allow on Paper", a budget's "Dry Run". */
  readonly name: string;
  /** Its one wording (PRD #2560 "Visual system and copy"): the lanes' own
   * words, and Dry Run's simulated cash. */
  readonly wording: string;
  /** As the Deploy button says it: "Deploy paper bot", "Deploy Dry Run bot". */
  readonly button: string;
}

/** Every word Deploy has for each world, in one place. */
export const DEPLOY_WORLDS = {
  dry_run: { name: 'Dry Run', wording: 'DRY RUN · simulated cash', button: 'Dry Run' },
  paper: { name: 'Paper', wording: LANE_MODE_WORDING.paper, button: 'paper' },
  shadow: { name: 'Shadow', wording: LANE_MODE_WORDING.shadow, button: 'shadow' },
  live: { name: 'Live', wording: LANE_MODE_WORDING.live, button: 'live' },
} as const satisfies Readonly<Record<DeployWorld, DeployWorldWords>>;

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
