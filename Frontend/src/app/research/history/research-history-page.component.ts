import {
  ChangeDetectionStrategy,
  Component,
  ElementRef,
  Injector,
  afterNextRender,
  computed,
  inject,
  input,
  linkedSignal,
  resource,
  viewChild,
} from '@angular/core';
import { ActivatedRoute, Router, RouterLink } from '@angular/router';

import { DateRangePickerComponent } from '../../shared/date-picker';
import { SymbolPickerComponent } from '../../shared/symbol-picker/symbol-picker.component';
import { TimestampDisplayComponent } from '../../shared/timestamp';
import type { TickerOption } from '../../shared/ticker-range-picker/ticker-range-picker.types';
import type { WorkspaceStateContent } from '../../shared/workspace/workspace-state';
import { WorkspaceHeaderComponent } from '../../shared/workspace/workspace-header.component';
import { WorkspaceStateBlockComponent } from '../../shared/workspace/workspace-state-block.component';
import {
  HISTORY_STATUSES,
  HISTORY_STATUS_IDS,
  HISTORY_TYPES,
  HISTORY_TYPE_IDS,
  ResearchHistoryService,
  ResearchHistoryUnavailableError,
  researchHistoryQuery,
  type ResearchHistoryEntry,
  type ResearchHistoryPage,
  type ResearchHistorySource,
  type ResearchHistoryUrl,
} from './research-history';

const PURPOSE =
  'Every study and run saved so far, from whichever store keeps it, newest first. Opening one reads what was saved; it never runs the study again.';

const NOT_RECORDED = 'not recorded';

/** Symbols named in a row before "and N more" (a universe can be long). */
const SYMBOLS_SHOWN = 3;

const RETRY = [{ id: 'retry', label: 'Try again' }] as const;

/** The sentences for the stores a page could not fully read: each says what is missing from the list. */
function gapSentences(sources: readonly ResearchHistorySource[]): string[] {
  return sources.flatMap((source) => {
    const found: string[] = [];
    if (source.state === 'unavailable' && source.error !== null) found.push(source.error);
    if (source.unreadable > 0) {
      const many = source.unreadable !== 1;
      found.push(
        `${source.unreadable} of the ${source.label} could not be read and ${many ? 'are' : 'is'} missing from this list.`,
      );
    }
    return found;
  });
}

/**
 * Research History (PRD #3490 R26): the saved studies and runs of every store, merged by the data plane into one
 * newest-first list. A store keeps its own records and its own detail page; this page only indexes them, so opening
 * an entry goes to the store's detail and never runs anything.
 *
 * The filters (kind, symbol, status, created between) and the page are the URL's, so a link or a reload lands on the
 * same list. A store that could not be read is named above the list with what is missing from it; when none could be
 * read there is no list at all, never an empty one.
 *
 * While the next page is read the last one stays on screen, marked busy; after paging, focus moves to the page count.
 */
@Component({
  selector: 'app-research-history-page',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [
    DateRangePickerComponent,
    RouterLink,
    SymbolPickerComponent,
    TimestampDisplayComponent,
    WorkspaceHeaderComponent,
    WorkspaceStateBlockComponent,
  ],
  templateUrl: './research-history-page.component.html',
  styleUrl: './research-history-page.component.scss',
})
export class ResearchHistoryPageComponent {
  // `ResearchHistoryUrl`'s values, bound by the router (`withComponentInputBinding`).
  readonly type = input<string>();
  readonly symbol = input<string>();
  readonly status = input<string>();
  readonly from = input<string>();
  readonly to = input<string>();
  readonly page = input<string>();

  private readonly service = inject(ResearchHistoryService);
  private readonly router = inject(Router);
  private readonly route = inject(ActivatedRoute);
  private readonly injector = inject(Injector);
  private readonly listStatus = viewChild<ElementRef<HTMLElement>>('listStatus');

  protected readonly purpose = PURPOSE;
  protected readonly notRecorded = NOT_RECORDED;
  protected readonly typeChoices = HISTORY_TYPE_IDS.map((id) => ({ id, label: HISTORY_TYPES[id].label }));
  protected readonly statusChoices = HISTORY_STATUS_IDS.map((id) => ({ id, label: HISTORY_STATUSES[id] }));
  protected readonly noRanges: readonly never[] = [];

  protected readonly query = computed(() =>
    researchHistoryQuery({
      type: this.type(),
      symbol: this.symbol(),
      status: this.status(),
      from: this.from(),
      to: this.to(),
      page: this.page(),
    }),
  );

  protected readonly history = resource({
    params: () => this.query(),
    loader: ({ params }) => this.service.read(params),
  });

  /** The page on screen: the newest read, kept while the next one loads. */
  protected readonly shown = linkedSignal<ResearchHistoryPage | undefined, ResearchHistoryPage | null>({
    source: () => (this.history.hasValue() ? this.history.value() : undefined),
    computation: (page, previous) => page ?? previous?.value ?? null,
  });

  /** Every store that was asked failed: the endpoint's refusal, with each store's reason. */
  private readonly refused = computed(() => {
    const error = this.history.error();
    return error instanceof ResearchHistoryUnavailableError ? error.sources : null;
  });

  /** The list could not be read for a reason that names no store (the data plane itself did not answer). */
  private readonly broken = computed(() => this.history.error() !== undefined && this.refused() === null);

  protected readonly entries = computed(() =>
    (this.broken() || this.refused() !== null ? [] : (this.shown()?.entries ?? [])).map((entry) => this.row(entry)),
  );

  protected readonly pageCount = computed(() => {
    const shown = this.shown();
    return shown === null ? 1 : Math.max(1, Math.ceil(shown.total / shown.page_size));
  });

  /** The symbol filter offers every symbol the stores hold in the chosen dates: a closed list the page owns (ADR 0066). */
  protected readonly symbolUniverse = computed<readonly TickerOption[]>(() =>
    (this.shown()?.symbols ?? []).map((symbol) => ({ symbol, name: symbol })),
  );

  /** The page-level state to say in words, or `null` when the list speaks for itself. */
  protected readonly notice = computed<WorkspaceStateContent | null>(() => {
    const refused = this.refused();
    if (refused !== null) {
      return {
        kind: 'unavailable',
        happened: 'History could not be read.',
        why: gapSentences(refused).join(' '),
        next: 'No store answered, so no list is shown rather than an empty one. Try again once they are back.',
        actions: RETRY,
      };
    }
    if (this.broken()) {
      return {
        kind: 'failed',
        happened: 'History could not be read.',
        why: 'The data plane did not answer.',
        next: 'Nothing was lost; the stores are as they were. Try again.',
        actions: RETRY,
      };
    }
    const shown = this.shown();
    if (shown === null || !shown.partial) return null;
    return {
      kind: 'partial',
      happened: `Showing ${shown.total} ${shown.total === 1 ? 'record' : 'records'} from the stores that could be read.`,
      why: gapSentences(shown.sources).join(' '),
      next: 'What these stores hold is missing from the list and from its count. Try again once they are back.',
      actions: RETRY,
    };
  });

  /** The list's one line of status, announced as it changes. */
  protected readonly statusText = computed(() => {
    const shown = this.shown();
    if (this.history.isLoading() && (shown === null || this.broken() || this.refused() !== null)) {
      return 'Reading the saved studies…';
    }
    if (shown === null || this.broken() || this.refused() !== null) return '';
    if (shown.total === 0) {
      return this.filtered() ? 'No saved record matches these filters.' : shown.partial ? '' : 'Nothing has been saved yet.';
    }
    if (shown.entries.length === 0) return `Page ${shown.page} is past the end: ${shown.total} records, ${this.pageCount()} pages.`;
    return `Page ${shown.page} of ${this.pageCount()} · ${shown.total} ${shown.total === 1 ? 'record' : 'records'}`;
  });

  protected readonly filtered = computed(() => {
    const query = this.query();
    return query.type !== null || query.symbol !== null || query.status !== null || query.from !== null || query.to !== null;
  });

  protected retry(): void {
    this.history.reload();
  }

  /** A changed filter starts the list again from its first page. */
  protected filter(param: keyof Omit<ResearchHistoryUrl, 'page'>, value: string | null): void {
    this.navigate({ [param]: value === '' ? null : value, page: null });
  }

  protected chooseDates(range: { readonly from: string; readonly to: string }): void {
    this.navigate({ from: range.from === '' ? null : range.from, to: range.to === '' ? null : range.to, page: null });
  }

  protected goToPage(page: number): void {
    this.navigate({ page: page === 1 ? null : String(page) });
    afterNextRender({ write: () => this.listStatus()?.nativeElement.focus() }, { injector: this.injector });
  }

  /** One entry as the table draws it. */
  private row(entry: ResearchHistoryEntry) {
    const kind = HISTORY_TYPES[entry.type];
    const parent = entry.parent === null ? null : { ...entry.parent, info: HISTORY_TYPES[entry.parent.type] };
    const extra = entry.symbols.length - SYMBOLS_SHOWN;
    return {
      key: `${entry.type}/${entry.native_id}`,
      entry,
      kind: kind.label,
      link: kind.detail === null ? null : kind.detail(entry.native_id),
      parent:
        parent === null || parent.info.detail === null
          ? null
          : { label: `${parent.info.label} ${parent.native_id.slice(0, 8)}`, link: parent.info.detail(parent.native_id) },
      symbols: entry.symbols.slice(0, SYMBOLS_SHOWN).join(', ') + (extra > 0 ? ` and ${extra} more` : ''),
      status: HISTORY_STATUSES[entry.status],
    };
  }

  private navigate(queryParams: { readonly [K in keyof ResearchHistoryUrl]?: string | null }): void {
    void this.router.navigate([], { relativeTo: this.route, queryParams, queryParamsHandling: 'merge' });
  }
}
