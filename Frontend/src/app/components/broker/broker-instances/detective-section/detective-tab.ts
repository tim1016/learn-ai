export type DetectiveTab = 'activity' | 'diagnostics';

const VALID: ReadonlySet<DetectiveTab> = new Set(['activity', 'diagnostics']);

/**
 * Resolves the active tab from a URL query parameter. Default is 'activity'.
 * Unrecognized or empty values fall back to the default so deep-links to
 * stale tab names degrade gracefully instead of rendering an empty body.
 */
export function deriveActiveTab(queryParam: string | null): DetectiveTab {
  if (!queryParam) return 'activity';
  return (VALID as Set<string>).has(queryParam) ? (queryParam as DetectiveTab) : 'activity';
}
