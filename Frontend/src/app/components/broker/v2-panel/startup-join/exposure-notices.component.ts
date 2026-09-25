import { ChangeDetectionStrategy, Component, input } from '@angular/core';

import type { ExposureNoticeView } from '../lib/broker-v2-panel.types';

/** What a startup refusal left at the broker (#2410); the copy is the backend's. */
@Component({
  selector: 'app-exposure-notices',
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    @if (notices().length > 0) {
      <ul class="exposure-notices" aria-label="What this refusal left at the broker">
        @for (notice of notices(); track notice.kind) {
          <li role="alert">
            <strong>{{ notice.label }}</strong>
            <span>{{ notice.explanation }}</span>
          </li>
        }
      </ul>
    }
  `,
  styles: `
    .exposure-notices {
      display: flex;
      flex-direction: column;
      gap: var(--space-1);
      margin: var(--space-1) 0 0;
      padding: 0;
      list-style: none;
    }

    li {
      display: flex;
      flex-direction: column;
      gap: 0.125rem;
      padding: var(--space-2);
      border-left: 3px solid var(--bear);
      background: var(--bg-surface);
    }

    span {
      color: var(--text-secondary);
    }
  `,
})
export class ExposureNoticesComponent {
  readonly notices = input.required<readonly ExposureNoticeView[]>();
}
