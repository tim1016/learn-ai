import { ChangeDetectionStrategy, Component } from '@angular/core';

/**
 * @deprecated Superseded by `MarkdownDrawerHostComponent`.
 *
 * This component is now a no-op stub kept so existing spec references compile.
 * The actual methodology drawer is rendered by the shell-level
 * `<app-markdown-drawer-host />` via `MarkdownDrawerService.open('methodology', anchor)`.
 *
 * Callers of the old `MethodologyDrawerService` API continue to work unchanged —
 * that service now delegates to `MarkdownDrawerService`.
 *
 * TODO: Remove this file once all test-imports are migrated to
 * `MarkdownDrawerHostComponent`.
 */
@Component({
  selector: 'app-methodology-drawer',
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: '',
})
export class MethodologyDrawerComponent {}
