/**
 * The stack of a thrown ``Error`` (or its name and message when it has
 * none), else the value pretty-printed as JSON. Used by the
 * ``<details>`` drawer in every error component so the technical
 * detail is consistent across the app.
 */
export function formatErrorDetails(err: unknown): string {
  if (err === null || err === undefined) return '';
  if (err instanceof Error) {
    return err.stack ?? `${err.name}: ${err.message}`;
  }
  try {
    return JSON.stringify(err, null, 2);
  } catch {
    return String(err);
  }
}
