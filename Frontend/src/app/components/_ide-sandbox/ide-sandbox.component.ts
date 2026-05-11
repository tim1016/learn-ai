import { ChangeDetectionStrategy, Component } from '@angular/core';

// Layout-regression sandbox for the .ide-grid primitive. Hosts three rails
// of dummy content tall enough to exercise sticky/scroll behavior. Used to
// eyeball breakpoints at 1100 / 1500 content-width and to render in Vitest
// as a smoke check that the global container query is in scope.
@Component({
  selector: 'app-ide-sandbox',
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <header class="sandbox-header">
      <h2>IDE layout sandbox</h2>
      <p class="muted">
        Resize the window. At &lt;1100px content width everything stacks.
        At ≥1100 the left rail and main go sticky; the right rail reflows
        beneath main. At ≥1500 the right rail promotes to a third sticky
        column.
      </p>
    </header>

    <div class="ide-grid" data-testid="ide-grid">
      <aside class="ide-rail-left" data-testid="ide-rail-left">
        @for (i of tall; track i) {
          <div class="tile tile--left">Left rail tile {{ i }}</div>
        }
      </aside>

      <section class="ide-main" data-testid="ide-main">
        @for (i of tall; track i) {
          <div class="tile tile--main">Main workspace tile {{ i }}</div>
        }
      </section>

      <aside class="ide-rail-right" data-testid="ide-rail-right">
        @for (i of tall; track i) {
          <div class="tile tile--right">Right rail tile {{ i }}</div>
        }
      </aside>
    </div>
  `,
  styles: [`
    :host { display: block; }

    .sandbox-header {
      margin-bottom: 1rem;
    }

    .tile {
      background: var(--bg-surface);
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 1rem;
      min-height: 6rem;
    }

    .tile--left  { border-left:  3px solid var(--accent); }
    .tile--main  { border-left:  3px solid var(--bull); }
    .tile--right { border-left:  3px solid var(--bear); }
  `],
})
export class IdeSandboxComponent {
  // 24 dummy tiles per rail — enough to force overflow at every breakpoint
  // so sticky/scroll behavior is observable.
  protected readonly tall = Array.from({ length: 24 }, (_, i) => i + 1);
}
