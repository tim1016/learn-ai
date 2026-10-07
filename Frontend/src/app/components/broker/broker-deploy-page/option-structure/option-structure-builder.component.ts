import { ChangeDetectionStrategy, Component, ElementRef, computed, input, linkedSignal, output, viewChild } from '@angular/core';

import type { ResourceTarget } from '../../../../fleet/resource-target';
import type { OptionStructureCatalogue } from '../../v2-panel/lib/broker-v2-panel.service';
import { OptionLegRowComponent } from './option-leg-row.component';
import { OptionPreviewPanelComponent } from './option-preview-panel.component';
import {
  addedRow,
  legRow,
  legsOf,
  previewRequest,
  structureNamed,
  structureStart,
  withoutRow,
  type LegRow,
  type NamedStructure,
  type NamedStructureStart,
  type OptionLegRules,
  type OptionOrderTerms,
} from './option-structure';
import { optionStructurePreview } from './option-structure-preview';
import { OptionVerdictComponent } from './option-verdict.component';

/** The structure the owner chose to use: its legs, and the name they still carry. */
export interface OptionStructureChoice {
  readonly structure: NamedStructure | null;
  readonly legs: readonly OptionLegRules[];
}

let nextBuilderId = 0;

/**
 * The structure builder (#2915): where a structure starts from, its legs,
 * and what they would pick right now. Three columns that stack in that order
 * on a narrow screen.
 *
 * Choosing a named structure replaces the legs with the catalogue's starting
 * legs for it. A structure keeps that name only while its legs are still
 * those; once one is edited it is Custom, and it is named again when they
 * match again. The verdict line is always there, in the server's words, and
 * "Use this structure" stays off while the server marks it red or a row is
 * unfinished.
 *
 * The preview is asked for the legs on screen while the builder is open, and
 * no longer once it is closed.
 */
@Component({
  selector: 'app-option-structure-builder',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [OptionLegRowComponent, OptionPreviewPanelComponent, OptionVerdictComponent],
  templateUrl: './option-structure-builder.component.html',
  styleUrl: './option-structure-builder.component.scss',
})
export class OptionStructureBuilderComponent {
  readonly catalogue = input.required<OptionStructureCatalogue>();
  readonly target = input.required<ResourceTarget>();
  /** The page's one name for its request for quotes. */
  readonly previewKey = input.required<string>();
  /** The legs the form holds, which the builder starts from. */
  readonly legs = input.required<readonly OptionLegRules[]>();
  readonly underlying = input.required<string>();
  /** What the bot listens to. */
  readonly listensTo = input.required<string>();
  /** How many whole structures the bot opens at an entry. */
  readonly structures = input.required<number>();
  readonly terms = input.required<OptionOrderTerms | null>();

  readonly use = output<OptionStructureChoice>();
  readonly cancelled = output();

  private readonly title = viewChild.required<ElementRef<HTMLElement>>('title');
  private readonly id = nextBuilderId++;
  protected readonly startTitleId = `option-builder-start-${this.id}`;
  protected readonly legsTitleId = `option-builder-legs-${this.id}`;
  protected readonly verdictId = `option-builder-verdict-${this.id}`;

  protected readonly rows = linkedSignal<LegRow[]>(() =>
    this.legs().map((leg) => legRow(leg, this.catalogue().strike_rules)),
  );
  /** The rows as legs, or `null` while one is unfinished. */
  private readonly whole = computed(() => legsOf(this.rows(), this.catalogue()));
  protected readonly named = computed(() => {
    const legs = this.whole();
    return legs === null ? null : structureNamed(legs, this.catalogue());
  });
  private readonly start = computed(() => structureStart(this.named(), this.catalogue()));
  protected readonly heading = computed(() => this.start()?.label ?? this.catalogue().custom_label);
  protected readonly description = computed(() => this.start()?.description ?? this.catalogue().custom_description);

  protected readonly preview = optionStructurePreview(() => {
    const legs = this.whole();
    const body = legs === null
      ? null
      : previewRequest(this.previewKey(), {
        underlying: this.underlying(),
        structure: this.named(),
        legs,
        listensTo: this.listensTo(),
        structures: this.structures(),
        terms: this.terms(),
      });
    return body === null ? null : { target: this.target(), body };
  });
  protected readonly verdict = computed(() => this.preview.answer()?.verdict ?? null);
  /** The legs the server's answer points at. */
  protected readonly flagged = computed(() => {
    const answer = this.preview.answer();
    return new Set([...(answer?.verdict.leg_ids ?? []), ...(answer?.leg_ids ?? [])]);
  });

  protected readonly canAdd = computed(() => this.rows().length < this.catalogue().max_legs);
  protected readonly canUse = computed(() => this.whole() !== null && this.verdict()?.red !== true);

  /** Moves the keyboard into the builder, to its heading. */
  focus(): void {
    this.title().nativeElement.focus();
  }

  protected startFrom(start: NamedStructureStart): void {
    this.rows.set(start.legs.map((leg) => legRow(leg, this.catalogue().strike_rules)));
  }

  /** Custom starts from no legs. A structure that is already the owner's own keeps them. */
  protected startCustom(): void {
    if (this.named() !== null) this.rows.set([]);
  }

  protected addLeg(): void {
    const row = this.canAdd() ? addedRow(this.rows(), this.catalogue()) : null;
    if (row !== null) this.rows.update((rows) => [...rows, row]);
  }

  protected setRow(legId: string, row: LegRow): void {
    this.rows.update((rows) => rows.map((each) => (each.leg_id === legId ? row : each)));
  }

  protected removeRow(legId: string): void {
    this.rows.update((rows) => withoutRow(rows, legId));
  }

  protected useIt(): void {
    const legs = this.whole();
    if (legs !== null && this.canUse()) this.use.emit({ structure: this.named(), legs });
  }
}
