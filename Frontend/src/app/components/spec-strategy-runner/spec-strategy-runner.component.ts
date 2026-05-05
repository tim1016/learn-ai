import { CommonModule } from '@angular/common';
import {
  ChangeDetectionStrategy,
  Component,
  computed,
  inject,
  signal,
} from '@angular/core';
import { FormsModule } from '@angular/forms';
import { SpecStrategyService } from '../../services/spec-strategy.service';
import {
  StrategySpec,
  SpecStrategyBacktestResult,
} from '../../graphql/spec-strategy-types';
import { CANONICAL_FIXTURES, CanonicalFixture } from './canonical-fixtures';
import { SNIPPET_GROUPS, SnippetGroup, SpecSnippet, insertSnippet } from './snippets';

/**
 * Minimal first-cut UI for running a declarative ``StrategySpec`` backtest.
 *
 * Phase 3c handed over the typed Apollo service; this component is the
 * minimum viable surface on top of it: pick one of the three canonical
 * fixtures (or paste your own JSON), adjust the run window / cash /
 * fill mode, fire the mutation, and read the trade log.
 *
 * Deliberately does NOT include:
 *   * a structured form editor for the spec (that's the bigger UI task)
 *   * fixture round-trip through GraphQL (the canonical fixtures are
 *     bundled as TS constants — see ``canonical-fixtures.ts``)
 *   * any chart visualisation (the JSON results panel is enough to
 *     prove the end-to-end path; charts can come later)
 *
 * The textarea preserves whatever JSON the user types — it's the
 * source of truth for the run, not the picker. Picker selection
 * just rewrites the textarea contents.
 */
@Component({
  selector: 'app-spec-strategy-runner',
  imports: [CommonModule, FormsModule],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './spec-strategy-runner.component.html',
  styleUrl: './spec-strategy-runner.component.scss',
})
export class SpecStrategyRunnerComponent {
  private readonly specService = inject(SpecStrategyService);

  readonly fixtures: readonly CanonicalFixture[] = CANONICAL_FIXTURES;

  /** Discoverable catalog: every indicator kind, condition kind, and
   * survival-rule template the evaluator currently supports.
   * Each entry has an example JSON snippet plus a Copy and Insert
   * button (rendered in the template).
   */
  readonly snippetGroups: readonly SnippetGroup[] = SNIPPET_GROUPS;

  /** Transient banner shown after a successful Copy / Insert action. */
  readonly catalogStatus = signal<string | null>(null);

  // ---- Form state -------------------------------------------------------
  readonly selectedFixtureId = signal<string>(CANONICAL_FIXTURES[0].id);
  readonly specJson = signal<string>(
    JSON.stringify(CANONICAL_FIXTURES[0].spec, null, 2),
  );
  readonly startDate = signal<string>('2024-03-28');
  readonly endDate = signal<string>('2024-12-31');
  readonly initialCash = signal<number>(100000);
  readonly fillMode = signal<'signal_bar_close' | 'next_bar_open'>(
    'signal_bar_close',
  );

  // ---- Run state --------------------------------------------------------
  readonly result = this.specService.result;
  readonly serviceError = this.specService.error;
  readonly loading = this.specService.loading;
  /** Local error from JSON parsing or input validation, separate from the service. */
  readonly localError = signal<string | null>(null);

  /** True iff a fixture is selected AND no edits have been made to the JSON. */
  readonly isPristine = computed<boolean>(() => {
    const fixture = this.fixtures.find((f) => f.id === this.selectedFixtureId());
    if (!fixture) return false;
    return this.specJson() === JSON.stringify(fixture.spec, null, 2);
  });

  /**
   * Number of trades, when a successful result is present.
   * Returned as a signal for the template's @let pattern.
   */
  readonly tradeCount = computed<number>(() => this.result()?.totalTrades ?? 0);

  // ---- Event handlers ---------------------------------------------------
  selectFixture(id: string): void {
    this.selectedFixtureId.set(id);
    const fixture = this.fixtures.find((f) => f.id === id);
    if (fixture) {
      this.specJson.set(JSON.stringify(fixture.spec, null, 2));
      this.localError.set(null);
    }
  }

  async runBacktest(): Promise<void> {
    this.localError.set(null);

    let parsed: StrategySpec;
    try {
      parsed = JSON.parse(this.specJson()) as StrategySpec;
    } catch (e) {
      this.localError.set(
        `Invalid JSON: ${e instanceof Error ? e.message : String(e)}`,
      );
      return;
    }

    try {
      await this.specService.runBacktest(parsed, {
        startDate: this.startDate(),
        endDate: this.endDate(),
        initialCash: this.initialCash(),
        fillMode: this.fillMode(),
      });
    } catch {
      // Service signal already captures the error — nothing else to do here.
    }
  }

  /** Pretty-printed example JSON for the catalog UI. */
  snippetJson(snippet: SpecSnippet): string {
    return JSON.stringify(snippet.example, null, 2);
  }

  /**
   * Insert a snippet into the spec textarea at the appropriate array
   * (indicators, entry.conditions, exit.conditions, or survival). On
   * JSON-parse failure surfaces the error in the local-error banner so
   * the user can fix their spec and retry.
   */
  insertSnippetIntoSpec(snippet: SpecSnippet): void {
    try {
      const next = insertSnippet(this.specJson(), snippet);
      this.specJson.set(next);
      this.localError.set(null);
      this.catalogStatus.set(`Inserted ${snippet.label}.`);
    } catch (e) {
      this.localError.set(e instanceof Error ? e.message : String(e));
      this.catalogStatus.set(null);
    }
  }

  /** Copy a snippet's JSON to the clipboard. Falls back to silent no-op
   * if the Clipboard API is unavailable (older browsers / non-secure
   * contexts) — the Insert button is the primary path anyway.
   */
  async copySnippetToClipboard(snippet: SpecSnippet): Promise<void> {
    const text = this.snippetJson(snippet);
    try {
      await navigator.clipboard.writeText(text);
      this.catalogStatus.set(`Copied ${snippet.label} to clipboard.`);
    } catch {
      this.catalogStatus.set(null);
    }
  }

  /** Format an int64 ms UTC timestamp for display in the trade table.
   *
   * Always renders in ``America/New_York`` regardless of browser locale —
   * per ``.claude/rules/numerical-rigor.md`` § "Timestamp rigor → UI
   * rendering", trading times are an ET concept and switching display
   * timezones based on viewer location creates ambiguity in screenshots
   * and shared analyses.
   */
  formatTime(ms: number): string {
    return new Date(ms).toLocaleString('en-US', {
      year: 'numeric',
      month: '2-digit',
      day: '2-digit',
      hour: '2-digit',
      minute: '2-digit',
      hour12: false,
      timeZone: 'America/New_York',
    });
  }

  /** Render an indicator-snapshot list compactly: "ema5=470.42, ema10=470.05". */
  formatIndicators(trade: SpecStrategyBacktestResult['trades'][0]): string {
    return trade.indicators
      .map((entry) => `${entry.name}=${entry.value.toFixed(4)}`)
      .join(', ');
  }
}
