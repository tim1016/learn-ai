import { ChangeDetectionStrategy, Component } from '@angular/core';

/**
 * @deprecated Superseded by `MarkdownDrawerHostComponent`.
 *
 * This component is now a no-op stub.  The actual broker-v2 help drawer is
 * rendered by the shell-level `<app-markdown-drawer-host />` via
 * `MarkdownDrawerService.open('broker-v2-manual', anchor)`.
 *
 * Callers of `BrokerV2HelpDrawerService` continue to work unchanged —
 * that service now delegates to `MarkdownDrawerService`.
 *
 * TODO: Remove this file once all test-imports are migrated to
 * `MarkdownDrawerHostComponent`.
 */
@Component({
  selector: 'app-broker-v2-help-drawer',
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: '',
})
export class BrokerV2HelpDrawerComponent {}
