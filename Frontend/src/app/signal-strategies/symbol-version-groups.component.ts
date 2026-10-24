import { LowerCasePipe, NgTemplateOutlet } from '@angular/common';
import { ChangeDetectionStrategy, Component, Directive, TemplateRef, contentChild, inject, input } from '@angular/core';

import { QUALIFICATION_LIST_LIMIT } from '../components/golden-search/golden-search.service';
import type { GoldenQualificationSummary } from '../components/golden-search/golden-search.types';
import { AssetIdentityComponent } from '../shared/asset-identity/asset-identity.component';
import { ReceiptLabelPipe } from '../shared/pipes/receipt-label.pipe';
import { TimestampDisplayComponent } from '../shared/timestamp/timestamp-display.component';
import { SignalStrategyChipComponent } from './signal-strategy-chip.component';
import { QUALIFICATION_TONES, versionSettingsText } from './signal-strategy-evidence';
import { SignalStrategyWorkspaceStore, type QualifiedVersions } from './signal-strategy-workspace.store';

/** The header cells a page adds after the shared columns: `<ng-template appVersionColumns><th>…</th></ng-template>`. */
@Directive({ selector: 'ng-template[appVersionColumns]' })
export class VersionColumnsDirective {
  readonly template = inject<TemplateRef<unknown>>(TemplateRef);
}

/** What a page's row cells are given: the version the row is for. */
export interface VersionRowContext {
  readonly $implicit: GoldenQualificationSummary;
}

/** The cells a page adds to each version's row: `<ng-template appVersionCells let-version><td>…</td></ng-template>`. */
@Directive({ selector: 'ng-template[appVersionCells]' })
export class VersionCellsDirective {
  readonly template = inject<TemplateRef<VersionRowContext>>(TemplateRef);

  static ngTemplateContextGuard(_directive: VersionCellsDirective, _context: unknown): _context is VersionRowContext {
    return true;
  }
}

/**
 * A strategy's qualified versions, one symbol at a time, in the one way every page
 * shows them: a version covers one symbol and one exact configuration, carries its
 * own status, and is current only while that status says ready. Nothing here sums a
 * symbol's versions into a strategy verdict.
 *
 * The group heading with its status counts, the version, status and configuration
 * columns, the empty note and the note that the read may have been cut short are
 * this component's, so a wording or status change lands once for Overview, Golden →
 * Versions and the header's Deploy-a-bot choice. It belongs to a strategy's workspace and reads why Golden
 * Search is ruled out from it. A page adds only its own columns (Golden's evidence and
 * re-proof, Deploy's use link) through the two templates, and handles the read being on
 * its way or failed before it renders this.
 */
@Component({
  selector: 'app-symbol-version-groups',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [
    AssetIdentityComponent,
    LowerCasePipe,
    NgTemplateOutlet,
    ReceiptLabelPipe,
    SignalStrategyChipComponent,
    TimestampDisplayComponent,
  ],
  templateUrl: './symbol-version-groups.component.html',
  styleUrl: './section-content.scss',
})
export class SymbolVersionGroupsComponent {
  /** The read, once it is ready. */
  readonly versions = input.required<QualifiedVersions>();

  /** Why no version can exist, when the registry rules Golden Search out for this strategy. */
  protected readonly goldenRefusal = inject(SignalStrategyWorkspaceStore).goldenRefusal;

  protected readonly columns = contentChild(VersionColumnsDirective);
  protected readonly cells = contentChild(VersionCellsDirective);

  protected readonly tones = QUALIFICATION_TONES;
  protected readonly listLimit = QUALIFICATION_LIST_LIMIT;

  protected settings(version: GoldenQualificationSummary): string {
    return versionSettingsText(version.parameters);
  }
}
