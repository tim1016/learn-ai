import { ChangeDetectionStrategy, Component, DestroyRef, effect, inject, input, signal, untracked } from '@angular/core';

import type { CandidateLoader, CandidatePageLoader } from './options-candidate-evidence.component';
import { OptionsGoldenSearchService, OptionsSearchDispatchError, optionsSearchErrorMessage } from './options-golden-search.service';
import type { OptionsStudy, SearchCommand, SessionLoader } from './options-golden-search.types';
import { OptionsSearchStudyComponent, type StudyAction } from './options-search-study.component';

const STEPS = ['Research', 'Compare and final test', 'Approval'];

/**
 * One golden search study, worked on: its research, the comparison and final
 * test, and the approval that publishes through the existing Golden Options
 * authority. It reads the study by its own id and sends every command with the
 * revision it read and a key that a retry reuses, so a repeated click, a
 * retried request or an older read of the study cannot start a second stage or
 * overwrite a newer revision.
 *
 * It does not choose a setup or plan a search: that is the Golden tab of a
 * saved setup. A study is the same wherever it is opened from.
 */
@Component({
  selector: 'app-options-study-workbench',
  imports: [OptionsSearchStudyComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './options-study-workbench.component.html',
  styleUrl: './options-golden-search.component.scss',
})
export class OptionsStudyWorkbenchComponent {
  private readonly service = inject(OptionsGoldenSearchService);
  private readonly destroyRef = inject(DestroyRef);

  readonly studyId = input.required<string>();

  protected readonly steps = STEPS;
  protected readonly candidatePageLoader: CandidatePageLoader = (studyId, offset) => this.service.candidatePage(studyId, offset);
  protected readonly candidateLoader: CandidateLoader = (studyId, digest, window, offset) => this.service.candidateEvidence(studyId, digest, window, offset);
  protected readonly sessionLoader: SessionLoader = (studyId, candidateHash, window, sessionOpenMs) => this.service.session(studyId, candidateHash, window, sessionOpenMs);
  protected readonly step = signal(3);
  protected readonly study = signal<OptionsStudy | null>(null);
  protected readonly error = signal<string | null>(null);
  protected readonly busy = signal(false);
  protected readonly loading = signal(false);
  private generation = 0;
  private readonly keys = new Map<string, string>();

  constructor() {
    effect(() => {
      const id = this.studyId();
      untracked(() => {
        // Another study is never shown under this id while it is read, and a late answer for the one before is dropped.
        if (this.study()?.id !== id) this.study.set(null);
        void this.loadStudy(id);
      });
    });
    effect((onCleanup) => {
      const value = this.study();
      if (value === null || !value.allowed_commands.includes('cancel')) return;
      const timer = setInterval(() => { if (!this.busy()) void this.refresh(); }, 2500);
      onCleanup(() => clearInterval(timer));
    });
    this.destroyRef.onDestroy(() => { this.generation += 1; });
  }

  protected chooseStep(step: number): void {
    this.step.set(step);
  }

  protected retry(): void {
    void this.loadStudy(this.studyId());
  }

  private async loadStudy(id: string): Promise<void> {
    const generation = ++this.generation;
    this.loading.set(true);
    this.error.set(null);
    try {
      const study = await this.service.get(id);
      if (generation !== this.generation) return;
      if (this.study()?.id === study.id && (this.study()?.revision ?? 0) > study.revision) return;
      this.study.set(study);
      this.step.set(['final', 'approval', 'approved'].includes(study.phase) || study.final_results.length > 0 ? 5 : study.phase === 'compare' || study.candidates.length > 0 ? 4 : 3);
    } catch (error) {
      if (generation === this.generation) this.error.set(optionsSearchErrorMessage(error, 'The study could not be loaded.'));
    } finally {
      if (generation === this.generation) this.loading.set(false);
    }
  }

  protected async refresh(): Promise<void> {
    const current = this.study();
    if (current === null) return;
    try {
      const value = await this.service.get(current.id);
      if (this.study()?.id === current.id && value.revision >= (this.study()?.revision ?? 0)) this.study.set(value);
    } catch (error) {
      this.error.set(optionsSearchErrorMessage(error, 'The latest study state could not be read.'));
    }
  }

  protected allowed(command: SearchCommand): boolean {
    return !this.busy() && this.study()?.allowed_commands.includes(command) === true;
  }

  protected async command(action: StudyAction): Promise<void> {
    const study = this.study();
    if (study === null || !this.allowed(action.command)) return;
    const { command, ...details } = action;
    this.busy.set(true);
    this.error.set(null);
    try {
      const value = await this.service.command(study.id, { command, expected_revision: study.revision, idempotency_key: this.key(`${study.id}:${study.revision}:${command}:${JSON.stringify(details)}`), ...details });
      if (this.study()?.id === value.id) this.study.set(value);
      if (command === 'open_final') this.step.set(5);
    } catch (error) {
      if (error instanceof OptionsSearchDispatchError && this.study()?.id === error.study.id) this.study.set(error.study);
      this.error.set(error instanceof OptionsSearchDispatchError ? error.message : optionsSearchErrorMessage(error, 'The command did not complete. Refresh to read the latest durable state.'));
    } finally {
      this.busy.set(false);
    }
  }

  /** One key per intent, so a retry of the same command at the same revision is the same request to the server. */
  private key(intent: string): string {
    const existing = this.keys.get(intent);
    if (existing !== undefined) return existing;
    const key = crypto.randomUUID();
    this.keys.set(intent, key);
    return key;
  }
}
