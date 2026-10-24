import { ChangeDetectionStrategy, Component, computed, inject } from '@angular/core';
import { RouterLink } from '@angular/router';

import { operationsTabRoute, type OperationsTab } from '../../operations/operations-tabs';
import { needsAttention, OptionArchiveService } from './option-archive.service';

/** Where the archive's evidence is: the Operations tab and the card on it. */
interface ArchiveTarget {
  readonly tab: OperationsTab;
  readonly fragment: string;
}

@Component({
  selector: 'app-option-archive-banner',
  imports: [RouterLink],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    @if (archive.unavailable() || archive.health()?.attention) {
      <aside>
        <p>{{ archive.unavailable() ? 'Archive status is unavailable.' : 'The option archive needs attention.' }}
          Your requested sessions are checked separately by Check data.</p>
        <a [routerLink]="route()" [fragment]="target().fragment">Inspect archive health</a>
      </aside>
    }
  `,
  styles: ['aside { margin-block: 1rem; padding: .75rem 1rem; border: 1px solid var(--p-orange-500); border-radius: var(--p-border-radius-md); }'],
})
export class OptionArchiveBannerComponent {
  readonly archive = inject(OptionArchiveService);
  protected readonly target = computed<ArchiveTarget>(() => {
    const health = this.archive.health();
    // No status to read: the Option archive tab is where it is retried.
    if (health === null) return { tab: 'option-archive', fragment: 'option-archive' };
    const underlying = health.underlyings.find((item) => needsAttention(item.severity));
    if (underlying) return { tab: 'option-archive', fragment: `option-archive-${underlying.underlying}` };
    return {
      tab: 'backups',
      fragment: health.backup.severity !== 'healthy' ? 'option-archive-backup' : 'option-archive-restore',
    };
  });
  protected readonly route = computed(() => operationsTabRoute(this.target().tab));
}
