import type { SetupLimits } from './options-backtest.types';

/**
 * What the page does with the limits the server serves (`GET /limits`). `null`
 * means they are not known yet or could not be read: the page then checks no
 * limit of its own and the server judges every body.
 */

/** Why a saved setup's name cannot be used, in the server's own sentence; null when it can, or when no rule is known. */
export function nameProblem(name: string, limits: SetupLimits | null): string | null {
  if (name === '' || limits === null) return null;
  const pattern = namePattern(limits.setup_name.pattern);
  if (pattern === null || pattern.test(name)) return null;
  return limits.setup_name.sentence;
}

/** Whether the setup already holds as many legs as the server allows; never, while the limit is not known. */
export function legsFull(legCount: number, limits: SetupLimits | null): boolean {
  return limits !== null && legCount >= limits.max_legs;
}

/** The served pattern as a regular expression; null when this browser cannot read it, and then the server judges the name. */
function namePattern(source: string): RegExp | null {
  try {
    return new RegExp(source);
  } catch (error) {
    if (error instanceof SyntaxError) return null;
    throw error;
  }
}
