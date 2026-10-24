import { ChangeDetectionStrategy, Component, computed, input, viewChildren } from '@angular/core';

import { WorkspaceStateBlockComponent } from '../../../../shared/workspace/workspace-state-block.component';
import { MemberLakeHoldingComponent } from './member-lake-holding.component';
import { describeUniverseHolding, type UniverseHoldingTally } from './universe-holding';

/**
 * How many member-sessions of stock bars the lake lacks for the universe, and each member's capture (#3524).
 *
 * The shared lake notice counts one instrument, so this composes it: one `app-member-lake-holding` per member, each
 * the shared count and capture unchanged, and a total above them. It warns and offers; it never starts a capture by
 * itself and never holds up Run study.
 */
@Component({
  selector: 'app-universe-lake-holding',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [WorkspaceStateBlockComponent, MemberLakeHoldingComponent],
  template: `
    <p class="text-sm text-color-secondary">
      A first run may be slow even when stock bars are held: option listings and contract bars must also be saved.
      A cold member outside the nightly archive can cost about 10–21 requests per session. Later runs reuse those files.
    </p>
    @if (summary(); as content) {
      <app-workspace-state-block [state]="content" />
    }
    @for (symbol of symbols(); track symbol) {
      <app-member-lake-holding [symbol]="symbol" [from]="from()" [to]="to()" />
    }
  `,
  styles: `
    :host {
      display: grid;
      gap: 0.75rem;
    }
  `,
})
export class UniverseLakeHoldingComponent {
  /** The universe as the run takes it: upper-case, one entry per member. */
  readonly symbols = input.required<readonly string[]>();
  readonly from = input.required<string>();
  readonly to = input.required<string>();

  private readonly members = viewChildren(MemberLakeHoldingComponent);

  private readonly tally = computed<UniverseHoldingTally>(() => {
    const members = this.members();
    let lacking = 0;
    let memberSessions = 0;
    for (const member of members) {
      const reading = member.holding.reading();
      if (reading.kind !== 'read' || reading.holding.toCapture === 0) continue;
      lacking += 1;
      memberSessions += reading.holding.toCapture;
    }
    return { members: members.length, lacking, memberSessions };
  });

  protected readonly summary = computed(() => describeUniverseHolding(this.tally()));

  /** Counts every member again, for a page whose own run has just filled the lake. */
  reread(): void {
    for (const member of this.members()) member.holding.reread();
  }
}
