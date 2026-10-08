import {
  ChangeDetectionStrategy,
  Component,
  ElementRef,
  Injector,
  afterNextRender,
  computed,
  inject,
  input,
  output,
  signal,
  viewChild,
} from '@angular/core';
import { Drawer } from 'primeng/drawer';

import type { ResourceTarget } from '../../../fleet/resource-target';
import { SymbolPickerComponent } from '../../../shared/symbol-picker/symbol-picker.component';
import type { OptionStructureCatalogue } from '../v2-panel/lib/broker-v2-panel.service';
import { OptionPreviewPanelComponent } from './option-structure/option-preview-panel.component';
import {
  legsInWords,
  previewNotAsked,
  previewRequest,
  structureStart,
  type DeployOptionSettings,
  type OptionPreviewSubject,
} from './option-structure/option-structure';
import {
  OptionStructureBuilderComponent,
  type OptionStructureChoice,
} from './option-structure/option-structure-builder.component';
import { newPreviewKey, optionStructurePreview } from './option-structure/option-structure-preview';
import { OptionVerdictComponent } from './option-structure/option-verdict.component';

let nextDrawerId = 0;

/**
 * Deploy's Options branch of "What it trades" (#2915): the underlying, the
 * chosen structure in a few lines with one Edit button, and what it would
 * pick right now.
 *
 * The structure is built in a drawer over the page, so the What column keeps
 * its place and its height. The drawer and this summary ask the preview
 * under one key, one at a time: the builder while it is open, this summary
 * otherwise, so a page never holds two sets of quote lines. Every sentence
 * here is the server's: the structure's description, the verdict, each state
 * of the preview and the warning that the structure leans against the
 * signal.
 */
@Component({
  selector: 'app-deploy-option-structure',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [Drawer, OptionPreviewPanelComponent, OptionStructureBuilderComponent, OptionVerdictComponent, SymbolPickerComponent],
  templateUrl: './deploy-option-structure.component.html',
  styleUrl: './deploy-option-structure.component.scss',
})
export class DeployOptionStructureComponent {
  readonly target = input.required<ResourceTarget>();
  readonly settings = input.required<DeployOptionSettings>();
  /** What the builder offers; `null` until it has been read. */
  readonly catalogue = input<OptionStructureCatalogue | null>(null);
  /** Why what the builder offers could not be read, when it could not. */
  readonly catalogueFailure = input<string | null>(null);
  /** What the bot listens to. */
  readonly listensTo = input.required<string>();
  /** How many whole structures the bot opens at an entry. */
  readonly structures = input.required<number>();

  readonly settingsChange = output<DeployOptionSettings>();
  readonly catalogueRetry = output();

  private readonly injector = inject(Injector);
  private readonly edit = viewChild<ElementRef<HTMLButtonElement>>('edit');
  private readonly builder = viewChild(OptionStructureBuilderComponent);

  protected readonly builderTitleId = `option-builder-drawer-title-${nextDrawerId++}`;
  /**
   * What the drawer is to a screen reader: a dialog over the page, named by
   * its heading. It holds the keyboard inside it, and the library's own role
   * for a drawer (a complementary landmark, with no name) says neither.
   */
  protected readonly builderDialog = {
    root: { role: 'dialog', 'aria-modal': 'true', 'aria-labelledby': this.builderTitleId },
  };

  /** This page's one name for its request for quotes, kept while Options stays chosen. */
  protected readonly previewKey = newPreviewKey();
  protected readonly building = signal(false);

  private readonly start = computed(() => {
    const catalogue = this.catalogue();
    return catalogue === null ? null : structureStart(this.settings().structure, catalogue);
  });
  protected readonly name = computed(() => this.start()?.label ?? this.catalogue()?.custom_label ?? '');
  /** The server's line for a named structure, or for Custom before it has a leg. */
  protected readonly description = computed(() => {
    const named = this.start();
    if (named !== null) return named.description;
    return this.settings().legs.length === 0 ? this.catalogue()?.custom_description ?? null : null;
  });
  /** A structure of the owner's own has no description: its legs say what it is. */
  protected readonly legs = computed(() => {
    const catalogue = this.catalogue();
    return catalogue === null || this.start() !== null ? [] : legsInWords(this.settings().legs, catalogue);
  });

  /** What the summary's preview is asked about: nothing before there is a leg, or before the catalogue is read. */
  private readonly subject = computed<OptionPreviewSubject | null>(() => {
    const { underlying, structure, legs, order_terms: terms } = this.settings();
    const catalogue = this.catalogue();
    return legs.length === 0 || catalogue === null
      ? null
      : { underlying, structure, legs, listensTo: this.listensTo(), structures: this.structures(), terms, offered: catalogue.order_terms };
  });
  /** Why the summary asks nothing about a structure that has legs, in the page's own words. */
  protected readonly notAsked = computed(() => {
    const subject = this.subject();
    return subject === null ? null : previewNotAsked(subject);
  });
  protected readonly preview = optionStructurePreview(() => {
    const subject = this.subject();
    // The builder asks while it is open.
    const body = this.building() || subject === null ? null : previewRequest(this.previewKey, subject);
    return body === null ? null : { target: this.target(), body };
  });
  /** Each leg in the server's own sentence, once a preview has answered for these legs. */
  protected readonly legsAsked = computed(() => this.preview.answer()?.legs_asked ?? []);
  /** The server's warning that the structure gains when the signal's symbol falls. */
  protected readonly leanWarning = computed(() => this.preview.answer()?.lean?.warning_sentence ?? null);

  protected setUnderlying(symbol: string): void {
    const underlying = symbol.trim().toUpperCase();
    if (underlying !== this.settings().underlying) this.settingsChange.emit({ ...this.settings(), underlying });
  }

  protected openBuilder(): void {
    this.building.set(true);
    afterNextRender({ write: () => this.builder()?.focus() }, { injector: this.injector });
  }

  /** Closes the builder and hands the keyboard back to the button that opened it. */
  protected closeBuilder(): void {
    this.building.set(false);
    afterNextRender({ write: () => this.edit()?.nativeElement.focus() }, { injector: this.injector });
  }

  protected useStructure(choice: OptionStructureChoice): void {
    this.settingsChange.emit({ ...this.settings(), structure: choice.structure, legs: choice.legs });
    this.closeBuilder();
  }
}
