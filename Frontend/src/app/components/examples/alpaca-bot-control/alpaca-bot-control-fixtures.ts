import type { components } from '../../../api/broker.types';
import type { JsonImported } from '../../../api/json-imported';
import accountUnattributable from '@repo-contracts/fixtures/alpaca-bot-control/v1/account_unattributable.json';
import accountUnprovable from '@repo-contracts/fixtures/alpaca-bot-control/v1/account_unprovable.json';
import entryPartialPending from '@repo-contracts/fixtures/alpaca-bot-control/v1/entry_partial_pending.json';
import exitCancellingEntry from '@repo-contracts/fixtures/alpaca-bot-control/v1/exit_cancelling_entry.json';
import exitClosing from '@repo-contracts/fixtures/alpaca-bot-control/v1/exit_closing.json';
import exitComplete from '@repo-contracts/fixtures/alpaca-bot-control/v1/exit_complete.json';
import instanceSubmitUncertain from '@repo-contracts/fixtures/alpaca-bot-control/v1/instance_submit_uncertain.json';
import knownManualExposure from '@repo-contracts/fixtures/alpaca-bot-control/v1/known_manual_exposure.json';
import marketDataStale from '@repo-contracts/fixtures/alpaca-bot-control/v1/market_data_stale.json';
import runningAttributedLong from '@repo-contracts/fixtures/alpaca-bot-control/v1/running_attributed_long.json';
import runningReadyFlat from '@repo-contracts/fixtures/alpaca-bot-control/v1/running_ready_flat.json';
import stopRequiresFlatten from '@repo-contracts/fixtures/alpaca-bot-control/v1/stop_requires_flatten.json';
import stoppedCarryoverIntact from '@repo-contracts/fixtures/alpaca-bot-control/v1/stopped_carryover_intact.json';
import stoppedCarryoverMismatch from '@repo-contracts/fixtures/alpaca-bot-control/v1/stopped_carryover_mismatch.json';
import stoppedFlat from '@repo-contracts/fixtures/alpaca-bot-control/v1/stopped_flat.json';

/** Mechanically derived from the Python Pydantic model through OpenAPI. */
export type AlpacaBotControlFixture = JsonImported<
  components['schemas']['AlpacaBotControlFixtureEnvelope']
>;

/** All committed, backend-authored static diagnostic examples. */
export const STATIC_ALPACA_BOT_CONTROL_FIXTURES = [
  runningReadyFlat,
  runningAttributedLong,
  entryPartialPending,
  exitCancellingEntry,
  exitClosing,
  exitComplete,
  stoppedFlat,
  stopRequiresFlatten,
  stoppedCarryoverIntact,
  stoppedCarryoverMismatch,
  instanceSubmitUncertain,
  marketDataStale,
  accountUnattributable,
  accountUnprovable,
  knownManualExposure,
] satisfies readonly AlpacaBotControlFixture[];
