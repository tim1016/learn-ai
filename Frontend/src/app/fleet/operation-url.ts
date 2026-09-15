/** Catalog-derived URL construction for clerk-scoped fleet operations (#2076, #2103).
 *
 * `clerk-scoped-url.ts`'s `laneUrl`/`accountUrl` take a free-form suffix, so
 * nothing ties a call site to a route the coordinator's operation catalog
 * actually declares — a typo in the suffix reaches the coordinator as a
 * silent 404. `operationUrl` builds the same clerk-scoped paths from the
 * committed catalog snapshot instead (`fleet-operation-catalog.snapshot.json`,
 * regenerated from `PythonDataService/app/broker/alpaca/clerk/fleet_adapter.py`
 * by `scripts/regenerate_fleet_operation_catalog_snapshot.py` — see that
 * script's docstring for the CI-enforced generate-and-diff contract):
 *
 * - it refuses an operation the catalog does not declare;
 * - it refuses an account-scoped operation invoked without an account;
 * - it substitutes a `:path` parameter (today only `manual_order_cancel`'s
 *   `order_ref`) WITHOUT `encodeURIComponent` — Starlette's `:path`
 *   converter matches across `/`, so percent-encoding the separator would
 *   under-match the route the coordinator mounts. Every other parameter,
 *   including `account_id`, is percent-encoded.
 */

import catalogSnapshot from './fleet-operation-catalog.snapshot.json';
import { clerkScope } from './clerk-scoped-url';

/** Every operation id the catalog snapshot declares — a compile-time-checked
 * union derived from the JSON import's own keys, so a call site naming an
 * operation the catalog does not declare fails to compile (an `as never`
 * cast is required to reach the runtime refusal path deliberately, exactly
 * as the "refuses an undeclared operation" test does). */
export type OperationId = keyof typeof catalogSnapshot.operations;

type CatalogOperation = (typeof catalogSnapshot.operations)[OperationId];

/** Every path parameter name the catalog's templates declare, mapped to the
 * camelCase field `operationUrl` callers carry its value under. A template
 * referencing a name missing from this map throws (see `requiredParam`)
 * rather than silently under-substituting. */
const PARAM_FIELD: Readonly<Record<string, keyof OperationTarget>> = {
  account_id: 'accountId',
  sid: 'sid',
  ticket_id: 'ticketId',
  order_ref: 'orderRef',
  profile_id: 'profileId',
  revision: 'revision',
  transaction_id: 'transactionId',
  external_order_id: 'externalOrderId',
  command_id: 'commandId',
  program_key: 'programKey',
};

/** `{name}` or `{name:converter}` — the converter (today only `path`) is
 * routing syntax, not part of the parameter identity (mirrors
 * `provider.py`'s `_PATH_PARAM_PATTERN` on the Python side). */
const PATH_PARAM_PATTERN = /\{([a-z_][a-z0-9_]*)(?::([a-z]+))?\}/g;

/** The identity plus every optional path-parameter value an `operationUrl`
 * call may need to supply, spread onto a `ResourceTarget`-shaped object at
 * the call site (e.g. `{ ...target, sid }`). */
export interface OperationTarget {
  readonly broker: string;
  readonly clerkId: string;
  readonly accountId?: string | null;
  readonly sid?: string;
  readonly ticketId?: string;
  readonly orderRef?: string;
  readonly profileId?: string;
  readonly revision?: string;
  readonly transactionId?: string;
  readonly externalOrderId?: string;
  readonly commandId?: string;
  readonly programKey?: string;
}

function lookupOperation(operationId: OperationId): CatalogOperation {
  // Widened for the one place a call site can still reach an operation id
  // outside the compile-time union above — an explicit `as never` cast at
  // the call site, exactly like the undeclared-operation contract test
  // does. The narrow static type on `operationId` is what makes that cast
  // necessary in the first place; this lookup is what turns the bypass into
  // a thrown error instead of `undefined` silently reaching the template
  // substitution below.
  const table = catalogSnapshot.operations as Readonly<Record<string, CatalogOperation>>;
  const operation = table[operationId];
  if (operation === undefined) {
    throw new Error(`operationUrl: ${JSON.stringify(operationId)} is not a declared fleet operation.`);
  }
  return operation;
}

function requiredParam(target: OperationTarget, paramName: string, operationId: OperationId): string {
  const field = PARAM_FIELD[paramName];
  if (field === undefined) {
    throw new Error(
      `operationUrl(${JSON.stringify(operationId)}): the catalog declares a {${paramName}} ` +
        'path parameter with no known OperationTarget field mapping.',
    );
  }
  const value = target[field];
  if (typeof value !== 'string' || value.trim().length === 0) {
    throw new Error(
      `operationUrl(${JSON.stringify(operationId)}) requires a ${paramName}, which was not provided.`,
    );
  }
  return value;
}

/** Build the clerk-scoped path for one catalog-declared fleet operation. */
export function operationUrl(operationId: OperationId, target: OperationTarget): string {
  const operation = lookupOperation(operationId);
  const path = operation.path_template.replace(
    PATH_PARAM_PATTERN,
    (_match: string, name: string, converter: string | undefined) => {
      const value = requiredParam(target, name, operationId);
      return converter === 'path' ? value : encodeURIComponent(value);
    },
  );
  return `${clerkScope(target)}${path}`;
}
