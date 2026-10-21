import { ChangeDetectionStrategy, Component, computed, inject } from '@angular/core';
import { RouterLink } from '@angular/router';

import { OptionArchiveService } from './option-archive.service';

@Component({
  selector: 'app-option-archive-banner',
  imports: [RouterLink],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    @if (archive.unavailable() || archive.health()?.attention) {
      <aside role="status">
        <p>{{ archive.unavailable() ? 'Archive status is unavailable.' : 'The option archive needs attention.' }}
          Your requested sessions are checked separately by Check data.</p>
        <a routerLink="/data-lab" [fragment]="target()">Inspect archive health</a>
      </aside>
    }
  `,
  styles: ['aside { margin-block: 1rem; padding: .75rem 1rem; border: 1px solid var(--p-orange-500); border-radius: var(--p-border-radius-md); }'],
})
export class OptionArchiveBannerComponent {
  readonly archive = inject(OptionArchiveService);
  readonly target = computed(() => {
    const health = this.archive.health();
    const underlying = health?.underlyings.find(item => ['at_risk', 'failed', 'unavailable'].includes(item.severity));
    return underlying ? `option-archive-${underlying.underlying}` :
      health?.backup.severity !== 'healthy' ? 'option-archive-backup' : 'option-archive-restore';
  });
}
