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

/**
 * #2106: the check above requires `withCommand(` AND a live `.lane(` read in
 * the SAME file. `alpaca-desk-account-data.service.ts` reads the directory
 * and hands the result to four *other* files as an `input()`-typed
 * `ResourceTarget` — each of those four mints `withCommand(` and contains
 * zero `.lane(` reads of its own, so the check above cannot see them at all.
 * The gap is invisible unless it is written down (this comment) and guarded
 * (the check below).
 *
 * This second check is narrower in a different way: instead of requiring a
 * live `.lane(` read in the same file, it requires an `input()`-typed
 * `ResourceTarget` in a file that also mints `withCommand(`, and demands
 * `fencedTarget(` or `freezeLaneFence(` also appear there — the two ways
 * this tree combines a live-identity target with a frozen fence, or freezes
 * one outright, before a command is minted. It is still a same-file textual
 * co-occurrence check, not a data-flow one, with the same two blind spots as
 * the check above (it cannot verify the fencing call actually gates the read
 * that reaches `withCommand(`, and a second, genuinely-unfenced target built
 * alongside a correctly-fenced one would still pass) plus a third of its
 * own: it cannot see whether the *provider* on the other end of the `input()`
 * already froze the whole target before handing it down. A provider that
 * does — the deploy drawer freezes an entire `ResourceTarget` once when it
 * opens and never re-derives it while open, the same "computed() frozen
 * once at action-open" pattern as the cohort-archive drawer above — makes
 * `fencedTarget(`/`freezeLaneFence(` in the *consumer* unnecessary. Those
 * consumers are named in `INPUT_ALLOWED` with the one-line proof this spec
 * demands; a future entry needs the same proof, not a bare addition.
 */
const INPUT_ALLOWED = new Set<string>([
  // `AlpacaDeployWorkflowComponent.target` is frozen once by
  // `AlpacaDeployDrawerComponent` when the drawer opens (`frozenTarget`,
  // guarded by `wasVisible`) and never re-derived from a live directory read
  // while the drawer stays open — proven by
  // `alpaca-deploy-drawer.component.spec.ts`'s "freezes the target at open
  // and does not re-derive it from a later input change".
  join('components', 'broker', 'broker-deploy-page', 'alpaca-deploy-workflow.component.ts'),
  // `DeployPaperAccessComponent.target` is the same already-frozen target,
  // one hop further down (`alpaca-deploy-workflow.component.html` passes
  // `deployTarget(view.account_id)`, a re-stamp of the frozen `target`, not a
  // fresh directory read).
  join('components', 'broker', 'broker-deploy-page', 'deploy-paper-access.component.ts'),
]);

describe('an input()-derived command target is fenced before it is minted', () => {
  it('every file that mints withCommand( from an input()-typed ResourceTarget also fences it, or is a named, proven exception', () => {
    const offenders: string[] = [];
    for (const path of sources(APP_ROOT)) {
      const text = readFileSync(path, 'utf8');
      if (!text.includes('withCommand(')) continue;
      if (!/\binput(?:\.required)?<ResourceTarget\b/.test(text)) continue;
      if (text.includes('fencedTarget(') || text.includes('freezeLaneFence(')) continue;
      offenders.push(path.slice(APP_ROOT.length + 1));
    }
    expect(offenders.filter((p) => !INPUT_ALLOWED.has(p))).toEqual([]);
  });
});
