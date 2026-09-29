import { DOCUMENT } from '@angular/common';
import { DestroyRef, inject, signal, type Signal } from '@angular/core';

/**
 * Whether `query` matches, as a signal that follows the viewport until the
 * calling component is destroyed. Call it in an injection context.
 *
 * For behavior that must change with the layout — a step that folds only
 * where the page stacks — not for styling, which belongs in the stylesheet.
 */
export function mediaQuerySignal(query: string): Signal<boolean> {
  const list = inject(DOCUMENT).defaultView?.matchMedia(query) ?? null;
  const matches = signal(list?.matches ?? false);
  if (list !== null) {
    const follow = (event: MediaQueryListEvent) => matches.set(event.matches);
    list.addEventListener('change', follow);
    inject(DestroyRef).onDestroy(() => list.removeEventListener('change', follow));
  }
  return matches.asReadonly();
}
