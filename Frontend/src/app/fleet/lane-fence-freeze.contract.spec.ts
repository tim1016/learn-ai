/** Once the directory can refresh mid-session, a command target rebuilt from a
 * live directory read is a live defect, not a latent one. This spec is the
 * guard: no non-spec source may mint a command from a directory lookup.
 *
 * Falsifiability: run against a pre-Task-3 checkout of this branch and it
 * must list `bot-panel-shell.component.ts`, `bots-list-page.component.ts`
 * and `bot-gallery-page.component.ts` as offenders — see
 * `docs/references/reconciliations` note for the exact command and output,
 * or the PR body for #2068.
 *
 * Known limitation: this is a file-level, textual co-occurrence check, not a
 * data-flow one. It passes as soon as `freezeLaneFence(` appears anywhere in
 * a file that also contains `withCommand(` and a live `.lane(` read — it does
 * not prove the freeze gates the *particular* read that reaches
 * `withCommand(`. A file that added a second, genuinely-unfenced lane read
 * alongside a correctly-fenced one would still pass. */
import { readFileSync, readdirSync } from 'node:fs';
import { join, sep } from 'node:path';
import { describe, expect, it } from 'vitest';

const APP_ROOT = join(__dirname, '..');

/**
 * No exception exists in the tree today: every file that both mints a
 * command (`withCommand(`) and reads the live directory
 * (`fleetDirectory.lane(` / `fleet.lane(`) also freezes it first
 * (`freezeLaneFence(`) before that read reaches `withCommand(` — whether via
 * the `linkedSignal` + `untracked()` + eager-`effect()` pattern (the panel,
 * the roster, the gallery) or a `computed()` frozen once at action-open and
 * never re-read for the command (the cohort-archive drawer).
 *
 * A future entry here needs the same proof this comment demands: a real,
 * audited reason the fence cannot be frozen in that file, with a one-line
 * comment saying why. An entry added only to make this spec pass without
 * that proof is the failure mode this spec exists to catch (#2068).
 */
const ALLOWED = new Set<string>();

/** The generated OpenAPI snapshot (37k+ lines, never hand-written) — skipped
 * for both cost and correctness: a schema field or doc comment could someday
 * coincidentally contain one of the matched substrings, and it can never be
 * the site of a real command-minting bug. */
const GENERATED_CONTRACT_FILE = join('api', 'broker.types.ts');

/** Testing doubles and fixtures are not production command-minting sites; a
 * double that fakes `fleetDirectory.lane(` (see `fleet-directory-testing.ts`)
 * must not be scanned as if it were one. */
function isTestingDouble(path: string): boolean {
  return path.split(sep).includes('testing') || /-testing\.ts$|\.fixtures\.ts$/.test(path);
}

function sources(dir: string): string[] {
  const found: string[] = [];
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    const path = join(dir, entry.name);
    if (entry.isDirectory()) {
      found.push(...sources(path));
      continue;
    }
    if (!entry.name.endsWith('.ts') || entry.name.endsWith('.spec.ts')) continue;
    if (isTestingDouble(path)) continue;
    if (path.endsWith(GENERATED_CONTRACT_FILE)) continue;
    found.push(path);
  }
  return found;
}

describe('the lane fence is frozen, never read at command time', () => {
  it('no component derives a command target from a directory lookup', () => {
    const offenders: string[] = [];
    for (const path of sources(APP_ROOT)) {
      const text = readFileSync(path, 'utf8');
      if (!text.includes('withCommand(')) continue;
      if (!/fleetDirectory\.lane\(|fleet\.lane\(/.test(text)) continue;
      if (text.includes('freezeLaneFence(')) continue;
      offenders.push(path.slice(APP_ROOT.length + 1));
    }
    expect(offenders.filter((p) => !ALLOWED.has(p))).toEqual([]);
  });
});
