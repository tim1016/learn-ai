import {
  ChangeDetectionStrategy,
  Component,
  ElementRef,
  afterNextRender,
  computed,
  input,
  linkedSignal,
  output,
  signal,
  viewChild,
} from '@angular/core';

import type { CustomGate, CustomGateInput } from '../lib/broker-v2-panel.types';
import type { GateVariableGroup } from './strategy-gates-model';

let nextGateEditorId = 0;

/** Catalogue chips shown at once; the search narrows the rest. */
const CATALOGUE_CHIPS_SHOWN = 18;

/**
 * Writes one custom Dark Bright Gate (#2639 D8): a name, one linear
 * expression over the strategy's variables, and the side of zero that is
 * bright.
 *
 * The browser never parses the expression: Preview asks the data plane to
 * judge the draft on the chart's candles, and its refusal (a variable it
 * does not know, a product of two variables) is shown in its words. Save
 * keeps the gate on the strategy, for every bot running it and Strategy Lab.
 */
@Component({
  selector: 'app-strategy-gate-editor',
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './strategy-gate-editor.component.html',
  styleUrl: './strategy-gate-editor.component.scss',
})
export class StrategyGateEditorComponent {
  /** The saved gate being edited, or `null` for a new one. */
  readonly gate = input<CustomGate | null>(null);
  readonly strategyName = input.required<string>();
  readonly variables = input.required<readonly GateVariableGroup[]>();
  /** The data plane's reason it refused the previewed draft. */
  readonly refusal = input<string | null>(null);
  /** The previewed draft is shading the chart. */
  readonly previewing = input(false);
  readonly save = input.required<(draft: CustomGateInput, gateId: string | null) => Promise<unknown>>();

  readonly previewRequested = output<CustomGateInput>();
  readonly closed = output();

  protected readonly ids = (() => {
    const base = `strategy-gate-editor-${nextGateEditorId++}`;
    return { title: `${base}-title`, name: `${base}-name`, expression: `${base}-expression`, sign: `${base}-sign` };
  })();

  protected readonly label = linkedSignal(() => this.gate()?.label ?? '');
  protected readonly expression = linkedSignal(() => this.gate()?.expression ?? '');
  protected readonly sign = linkedSignal<'gt' | 'lt'>(() => this.gate()?.sign ?? 'gt');
  protected readonly catalogueQuery = signal('');
  /** The draft last sent to Preview: its answer is only shown while the fields still match it. */
  private readonly previewed = signal<CustomGateInput | null>(null);
  protected readonly saving = signal(false);
  protected readonly saveError = signal<string | null>(null);

  private readonly nameInput = viewChild.required<ElementRef<HTMLInputElement>>('nameInput');
  private readonly expressionInput = viewChild.required<ElementRef<HTMLInputElement>>('expressionInput');

  constructor() {
    // The editor replaces the button that opened it; focus starts at the name.
    afterNextRender(() => this.nameInput().nativeElement.focus());
  }

  protected readonly draft = computed((): CustomGateInput | null => {
    const label = this.label().trim();
    const expression = this.expression().trim();
    return label === '' || expression === '' ? null : { label, expression, sign: this.sign() };
  });

  /** `'current'` while the fields match the previewed draft, `'changed'` once edited after, else `null`. */
  protected readonly previewState = computed((): 'current' | 'changed' | null => {
    const previewed = this.previewed();
    if (previewed === null) return null;
    const draft = this.draft();
    const same = draft !== null
      && draft.label === previewed.label
      && draft.expression === previewed.expression
      && draft.sign === previewed.sign;
    return same ? 'current' : 'changed';
  });

  /** Every group as given, the catalogue narrowed by the search. */
  protected readonly groups = computed(() => {
    const query = this.catalogueQuery().trim().toLowerCase();
    return this.variables().map((group) => {
      if (group.title !== 'Catalogue') return { ...group, searchable: false };
      const matches = group.chips.filter(
        (chip) => query === '' || chip.name.toLowerCase().includes(query) || chip.hint.toLowerCase().includes(query),
      );
      return { ...group, searchable: true, chips: matches.slice(0, CATALOGUE_CHIPS_SHOWN) };
    });
  });

  protected textOf(event: Event): string {
    return event.target instanceof HTMLInputElement ? event.target.value : '';
  }

  /** Puts `name` at the cursor in the expression, spaced from its neighbours. */
  protected insert(name: string): void {
    const field = this.expressionInput().nativeElement;
    const text = this.expression();
    const start = field.selectionStart ?? text.length;
    const end = field.selectionEnd ?? start;
    const before = text.slice(0, start);
    const after = text.slice(end);
    const lead = before === '' || before.endsWith(' ') ? '' : ' ';
    const trail = after.startsWith(' ') ? '' : ' ';
    const next = `${before}${lead}${name}${trail}${after}`;
    const cursor = before.length + lead.length + name.length + trail.length;
    this.expression.set(next);
    field.value = next;
    field.focus();
    field.setSelectionRange(cursor, cursor);
  }

  protected preview(): void {
    const draft = this.draft();
    if (draft === null) return;
    this.previewed.set(draft);
    this.previewRequested.emit(draft);
  }

  protected async submit(): Promise<void> {
    const draft = this.draft();
    if (draft === null || this.saving()) return;
    this.saving.set(true);
    this.saveError.set(null);
    try {
      await this.save()(draft, this.gate()?.gate_id ?? null);
      this.closed.emit();
    } catch (error) {
      this.saveError.set(error instanceof Error ? error.message : 'The gate could not be saved.');
    } finally {
      this.saving.set(false);
    }
  }
}
