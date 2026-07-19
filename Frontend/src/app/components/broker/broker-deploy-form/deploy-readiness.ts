export type ActionPlanDeployReasonCode =
  | 'ACTION_PLAN_EMPTY'
  | 'ACTION_PLAN_ENTRY_LEG_REQUIRED'
  | 'ACTION_PLAN_UNSUPPORTED'
  | 'ACTION_PLAN_CLOSE_LEG_REQUIRED';

export interface ActionPlanDeployReadiness {
  canDeploy: boolean;
  reasonCode: ActionPlanDeployReasonCode | null;
  message: string;
}

interface ActionPlanEnvelope {
  on_enter: unknown[];
  on_exit: unknown[];
}

interface ReadableActionPlanEntry {
  legId: string;
  instrumentKind: string;
  position: string;
}

const ACTION_PLAN_REQUIRED_STRATEGIES = new Set([
  'deployment_validation',
  'ema_crossover_signal',
]);
const ACTION_PLAN_READY: ActionPlanDeployReadiness = {
  canDeploy: true,
  reasonCode: null,
  message: 'Action plan is ready for deployment.',
};

export function actionPlanDeployReadiness(
  strategyKey: string,
  actionPlan: unknown,
): ActionPlanDeployReadiness {
  const normalizedStrategyKey = strategyKey.trim();
  if (!ACTION_PLAN_REQUIRED_STRATEGIES.has(normalizedStrategyKey)) {
    return ACTION_PLAN_READY;
  }
  const strategyLabel = actionPlanStrategyLabel(normalizedStrategyKey);
  if (!hasActionPlanEnvelope(actionPlan)) {
    return {
      canDeploy: false,
      reasonCode: 'ACTION_PLAN_EMPTY',
      message: `${strategyLabel} requires an action plan with one long stock entry leg and a matching close leg before deployment.`,
    };
  }
  const hasEntries = actionPlan.on_enter.length > 0;
  const hasExits = actionPlan.on_exit.length > 0;
  if (!hasEntries && !hasExits) {
    return {
      canDeploy: false,
      reasonCode: 'ACTION_PLAN_EMPTY',
      message: `${strategyLabel} requires an action plan; ON ENTER and ON EXIT are both empty.`,
    };
  }
  if (!hasEntries) {
    return {
      canDeploy: false,
      reasonCode: 'ACTION_PLAN_ENTRY_LEG_REQUIRED',
      message: `${strategyLabel} requires at least one ON ENTER entry leg.`,
    };
  }
  const firstEntry = readableFirstEntry(actionPlan);
  if (firstEntry === null) {
    return {
      canDeploy: false,
      reasonCode: 'ACTION_PLAN_UNSUPPORTED',
      message: `${strategyLabel} cannot consume this action-plan shape. Use one long stock entry leg with a close-leg exit.`,
    };
  }
  if (
    actionPlan.on_enter.length !== 1 ||
    firstEntry.instrumentKind !== 'stock' ||
    firstEntry.position !== 'long'
  ) {
    return {
      canDeploy: false,
      reasonCode: 'ACTION_PLAN_UNSUPPORTED',
      message: `${strategyLabel} currently supports exactly one long stock entry leg. Option, short, and multi-leg plans are not deployable on this runtime path yet.`,
    };
  }
  if (!actionPlan.on_exit.some((exit) => closeLegReferences(exit, firstEntry.legId))) {
    return {
      canDeploy: false,
      reasonCode: 'ACTION_PLAN_CLOSE_LEG_REQUIRED',
      message: `${strategyLabel} requires an ON EXIT close leg for the entry leg '${firstEntry.legId}'.`,
    };
  }
  return ACTION_PLAN_READY;
}

function actionPlanStrategyLabel(strategyKey: string): string {
  return strategyKey === 'ema_crossover_signal'
    ? 'EMA Crossover Signal'
    : 'Deployment Validation';
}

function hasActionPlanEnvelope(value: unknown): value is ActionPlanEnvelope {
  if (value === null || typeof value !== 'object') return false;
  return 'on_enter' in value && 'on_exit' in value && Array.isArray(value.on_enter) && Array.isArray(value.on_exit);
}

function readableFirstEntry(actionPlan: ActionPlanEnvelope): ReadableActionPlanEntry | null {
  const entry = actionPlan.on_enter[0];
  if (entry === null || typeof entry !== 'object') return null;
  if (!('leg_id' in entry) || !('instrument' in entry) || !('position' in entry)) {
    return null;
  }
  const instrument = entry.instrument;
  if (instrument === null || typeof instrument !== 'object' || !('kind' in instrument)) {
    return null;
  }
  if (typeof entry.leg_id !== 'string' || typeof instrument.kind !== 'string') {
    return null;
  }
  return typeof entry.position === 'string'
    ? { legId: entry.leg_id, instrumentKind: instrument.kind, position: entry.position }
    : null;
}

function closeLegReferences(exit: unknown, entryLegId: string): boolean {
  return (
    exit !== null &&
    typeof exit === 'object' &&
    'entry_leg_id' in exit &&
    exit.entry_leg_id === entryLegId
  );
}
