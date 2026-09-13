/** Immutable, provenance-bearing address of one routed resource (FR-094/095).
 *
 * Targets are created from a rendered lane/resource and retained by the caller
 * for the life of a read, dialog, or retry. They deliberately contain no
 * directory lookup: resolving a default lane at submission time would let an
 * operator's command cross a binding or routing-epoch change.
 */

/** Closed fleet capability vocabulary, mirrored from the coordinator's
 * `Capability` enum. Widen it only when the protocol does. */
export type FleetCapability =
  | 'account_read'
  | 'positions_read'
  | 'orders_read'
  | 'market_status_read'
  | 'configuration_manage'
  | 'bot_panel_read'
  | 'bot_action'
  | 'deploy'
  | 'custody_read'
  | 'custody_command'
  | 'gallery_read'
  | 'manual_orders'
  | 'stream_subscribe';

export interface ResourceTarget {
  readonly broker: string;
  readonly clerkId: string;
  readonly accountId: string | null;
  readonly entityId: string | null;
  readonly capability: FleetCapability | null;
  readonly idempotencyKey: string | null;
  readonly bindingGeneration: number | null;
  /** The lane routing epoch observed when this target was frozen. */
  readonly routingEpoch: number | null;
}

/** The §10.3 command envelope a mutating clerk-scoped request carries. */
export interface CommandContext {
  capability: FleetCapability;
  idempotency_key?: string;
  expected_effective_binding_generation?: number;
  target?: {
    account_id?: string;
    entity_id?: string;
  };
}

interface FrozenTargetDimensions {
  accountId?: string | null;
  entityId?: string | null;
  capability?: FleetCapability | null;
  idempotencyKey?: string | null;
  bindingGeneration?: number | null;
  routingEpoch?: number | null;
}

function requiredIdentity<T extends string>(value: T, name: string): T {
  if (value.trim().length === 0) {
    throw new Error(`A resource target requires a non-empty ${name}.`);
  }
  return value;
}

function optionalIdentity<T extends string>(
  value: T | null | undefined,
  name: string,
): T | null {
  if (value === null || value === undefined) return null;
  return requiredIdentity(value, name);
}

function optionalGeneration(value: number | null | undefined, name: string): number | null {
  if (value === null || value === undefined) return null;
  if (!Number.isInteger(value) || value < 0) {
    throw new Error(`A resource target requires a non-negative integer ${name}.`);
  }
  return value;
}

/** Freeze an address from the resource that was rendered to the operator. */
export function resourceTarget(
  broker: string,
  clerkId: string,
  frozen: FrozenTargetDimensions = {},
): ResourceTarget {
  return Object.freeze({
    broker: requiredIdentity(broker, 'broker'),
    clerkId: requiredIdentity(clerkId, 'clerk ID'),
    accountId: optionalIdentity(frozen.accountId, 'account ID'),
    entityId: optionalIdentity(frozen.entityId, 'entity ID'),
    capability: optionalIdentity(frozen.capability, 'capability'),
    idempotencyKey: optionalIdentity(frozen.idempotencyKey, 'idempotency key'),
    bindingGeneration: optionalGeneration(frozen.bindingGeneration, 'binding generation'),
    routingEpoch: optionalGeneration(frozen.routingEpoch, 'routing epoch'),
  });
}

/** Derive the same resource with one dimension replaced — the result is a
 * new frozen target, never an in-place mutation. */
export function withAccount(target: ResourceTarget, accountId: string | null): ResourceTarget {
  return Object.freeze({ ...target, accountId: optionalIdentity(accountId, 'account ID') });
}

export function withEntity(target: ResourceTarget, entityId: string | null): ResourceTarget {
  return Object.freeze({ ...target, entityId: optionalIdentity(entityId, 'entity ID') });
}

/** Derive a command address without making callers repeat every frozen
 * provenance field. The interaction owner still decides when to mint and
 * retain the durable identity. */
export function withCommand(
  target: ResourceTarget,
  capability: FleetCapability,
  idempotencyKey: string | null,
): ResourceTarget {
  return resourceTarget(target.broker, target.clerkId, {
    accountId: target.accountId,
    entityId: target.entityId,
    capability,
    idempotencyKey,
    bindingGeneration: target.bindingGeneration,
    routingEpoch: target.routingEpoch,
  });
}

/** The §10.3 envelope for a mutating request, built strictly from the frozen
 * target: capability, durable identity, binding-generation fence and the
 * target identities. Absent optional dimensions are omitted, not nulled. */
export function commandContextOf(target: ResourceTarget): CommandContext {
  const capability = optionalIdentity(target.capability, 'capability');
  if (capability === null) {
    throw new Error('A command context requires a frozen capability.');
  }
  const context: CommandContext = { capability };
  if (target.idempotencyKey !== null) {
    context.idempotency_key = target.idempotencyKey;
  }
  if (target.bindingGeneration !== null) {
    context.expected_effective_binding_generation = target.bindingGeneration;
  }
  const commandTarget: NonNullable<CommandContext['target']> = {};
  if (target.accountId !== null) {
    commandTarget.account_id = target.accountId;
  }
  if (target.entityId !== null) {
    commandTarget.entity_id = target.entityId;
  }
  if (Object.keys(commandTarget).length > 0) {
    context.target = commandTarget;
  }
  return context;
}

/** The body a mutating clerk-scoped request sends: the provider payload
 * plus the envelope, exactly as the coordinator's contract expects. */
export function commandBodyOf(target: ResourceTarget, payload: object): object {
  return { ...payload, command_context: commandContextOf(target) };
}

/** The lane key every fleet-scoped cache, cursor and dialog is keyed by. */
export function laneKey(
  broker: string,
  clerkId: string,
  routingEpoch: number | null = null,
  bindingGeneration: number | null = null,
  accountId: string | null = null,
): string {
  const target = resourceTarget(broker, clerkId, {
    routingEpoch,
    bindingGeneration,
    accountId,
  });
  return [
    target.broker,
    target.clerkId,
    target.routingEpoch ?? 'unknown-epoch',
    target.bindingGeneration ?? 'unknown-binding',
    target.accountId ?? 'lane',
  ].join('::');
}
