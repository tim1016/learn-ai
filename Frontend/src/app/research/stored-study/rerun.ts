import { effect, inject, signal, untracked, type Signal } from '@angular/core';

import { StoredInputs, StoredInputsError } from './stored-inputs';
import {
  StoredStudyNotFoundError,
  StoredStudyService,
  StoredStudyUnreadableError,
  type StoredStudyType,
} from './stored-study';

/** What a study page says about the saved run its inputs were filled in from. */
export type StudyPrefill =
  | {
      readonly state: 'filled';
      readonly study: StoredStudyType;
      readonly id: string;
      /** When the saved run started, `int64 ms UTC`. */
      readonly savedAtMs: number;
    }
  | { readonly state: 'failed'; readonly message: string };

export interface RerunPrefill {
  /** The page's notice about the run it was filled from; `null` once a run has started, or when nothing was asked. */
  readonly notice: Signal<StudyPrefill | null>;
  /** The saved run the next run is a rerun of: what the run's request names as `rerun_of`; `null` when not filled. */
  parent(): string | null;
  /**
   * The run was accepted, so the fill has done its job: the notice goes and a later run is not a rerun of this one.
   * A run that fails to start leaves the fill, and its lineage, in place for the next press.
   */
  started(): void;
}

/**
 * Rerun (PRD #3490 R26): a study page opened with `?rerun=<saved run id>` fills its inputs from that run and runs
 * nothing. Pressing Run study is the explicit action; the new run is a new record that names this one.
 *
 * `apply` receives the saved run's inputs as the study's request held them and sets the page's own signals; it throws
 * `StoredInputsError` for a field it cannot read, and the notice then says the run's inputs could not be read, with the
 * page left as it was. The run's own data is not fetched or reused: a run started from a fill reads whatever the data
 * is then, and has the data identity of that.
 *
 * Call it in a component's constructor (it injects and registers an effect).
 */
export function createRerunPrefill(options: {
  readonly study: StoredStudyType;
  /** The `rerun` query parameter, bound to a component input. */
  readonly id: Signal<string | undefined>;
  readonly apply: (inputs: StoredInputs) => void;
}): RerunPrefill {
  const service = inject(StoredStudyService);
  const notice = signal<StudyPrefill | null>(null);
  let parent: string | null = null;
  let latest = 0;

  async function fill(id: string): Promise<void> {
    const attempt = ++latest;
    try {
      const record = await service.read(options.study, id);
      if (attempt !== latest) return;
      options.apply(new StoredInputs(record.inputs));
      parent = id;
      notice.set({ state: 'filled', study: options.study, id, savedAtMs: record.created_at_ms });
    } catch (error) {
      if (attempt !== latest) return;
      parent = null;
      notice.set({ state: 'failed', message: describe(error) });
    }
  }

  effect(() => {
    const id = options.id();
    if (id === undefined || id === '') return;
    untracked(() => void fill(id));
  });

  return {
    notice: notice.asReadonly(),
    parent: () => parent,
    started() {
      parent = null;
      latest++;
      notice.set(null);
    },
  };
}

function describe(error: unknown): string {
  if (error instanceof StoredStudyNotFoundError) {
    return 'The saved run to fill in from was not found, so nothing was filled in.';
  }
  if (error instanceof StoredStudyUnreadableError) {
    return 'The saved run to fill in from could not be read, so nothing was filled in.';
  }
  if (error instanceof StoredInputsError) {
    return `The saved run's inputs could not be read (${error.message}), so nothing was filled in.`;
  }
  return 'The saved run to fill in from could not be opened, so nothing was filled in. Try again.';
}
