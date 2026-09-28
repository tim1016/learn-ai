import { ChangeDetectionStrategy, Component, computed, input } from '@angular/core';
import { RouterLink } from '@angular/router';

import {
  accountWorkspaceFixRoute,
  type AccountWorkspaceLink,
  type BoundAccountWorkspaceAddress,
} from '../../../fleet/account-workspace';
import type { LaneAttentionItem } from '../../../services/lane-attention.service';

interface AttentionLine {
  readonly item: LaneAttentionItem;
  /** Severity in words beside its icon: never colour alone (WCAG 1.4.1). */
  readonly severity: 'Blocking' | 'Warning';
  readonly fix: AccountWorkspaceLink | null;
}

/**
 * Home's attention lines (PRD #2560): one plain line per thing the owner must
 * act on, each with its one fix. The headline and the fix are the lane's own
 * (`LaneAttentionRead`); this only turns the fix's destination into a link
 * inside the account's workspace. An unknown read says so rather than looking
 * quiet.
 */
@Component({
  selector: 'app-home-attention',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [RouterLink],
  templateUrl: './home-attention.component.html',
  styleUrl: './home-attention.component.scss',
})
export class HomeAttentionComponent {
  readonly items = input.required<readonly LaneAttentionItem[]>();
  /** The lane's attention read could not be completed. */
  readonly unknown = input(false);
  readonly account = input.required<BoundAccountWorkspaceAddress>();

  protected readonly lines = computed<readonly AttentionLine[]>(() =>
    this.items().map((item) => ({
      item,
      severity: item.severity === 'blocking' ? 'Blocking' : 'Warning',
      fix: accountWorkspaceFixRoute(this.account(), item.action.destination, item.strategy_instance_id ?? null),
    })),
  );
}
