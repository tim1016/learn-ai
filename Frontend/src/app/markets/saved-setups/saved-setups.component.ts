import { ChangeDetectionStrategy, Component, inject, output, signal } from '@angular/core';

import { AssetIdentityComponent } from '../../shared/asset-identity';
import { TimestampDisplayPipe } from '../../shared/timestamp';
import type { DataLabSessionSummary } from '../../services/data-lab-session.service';
import { SavedSetupsService } from './saved-setups.service';

/**
 * The saved setups an instrument place offers: save the workspace, update or
 * replace the setup it was opened from, rename and delete a setup, and open
 * one under its own instrument. Every call and every rule is the service's;
 * this holds only what a row being renamed needs.
 */
@Component({
  selector: 'app-saved-setups',
  imports: [AssetIdentityComponent, TimestampDisplayPipe],
  templateUrl: './saved-setups.component.html',
  styleUrl: './saved-setups.component.scss',
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class SavedSetupsComponent {
  protected readonly setups = inject(SavedSetupsService);

  /** The drawer was asked to close, or a setup opened and no longer needs it. */
  readonly closed = output();

  protected readonly renamingId = signal<string | null>(null);
  protected readonly renameValue = signal('');

  constructor() {
    // Opening the drawer reads the list: the summaries, never a whole record.
    void this.setups.refresh();
  }

  protected async open(id: string): Promise<void> {
    await this.setups.open(id);
    if (this.setups.error() === null) this.closed.emit();
  }

  protected onNameInput(event: Event): void {
    if (event.target instanceof HTMLInputElement) this.setups.name.set(event.target.value);
  }

  protected startRenaming(session: DataLabSessionSummary): void {
    this.renamingId.set(session.id);
    this.renameValue.set(session.name);
  }

  protected onRenameInput(event: Event): void {
    if (event.target instanceof HTMLInputElement) this.renameValue.set(event.target.value);
  }

  protected async confirmRename(id: string): Promise<void> {
    const name = this.renameValue().trim();
    if (name !== '') await this.setups.rename(id, name);
    this.renamingId.set(null);
  }
}
