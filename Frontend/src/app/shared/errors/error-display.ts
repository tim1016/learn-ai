import { GraphqlError } from '../graphql/graphql-error';

/**
 * Pretty-printed JSON of the underlying GraphQL error payloads, or
 * the message + stack of any other thrown value. Used by the
 * ``<details>`` drawer in every error component so the technical
 * detail is consistent across the app.
 */
export function formatErrorDetails(err: unknown): string {
  if (err === null || err === undefined) return '';
  if (err instanceof GraphqlError) {
    return JSON.stringify(
      { context: err.context ?? null, errors: err.errors },
      null,
      2,
    );
  }
  if (err instanceof Error) {
    return err.stack ?? `${err.name}: ${err.message}`;
  }
  try {
    return JSON.stringify(err, null, 2);
  } catch {
    return String(err);
  }
}
