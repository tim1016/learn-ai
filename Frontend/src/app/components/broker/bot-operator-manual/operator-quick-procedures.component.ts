import { ChangeDetectionStrategy, Component } from '@angular/core';
import { RouterLink } from '@angular/router';

import { documentAnchor } from '../../../shared/markdown/markdown-slug';

interface QuickProcedure {
  readonly id: string;
  readonly eyebrow: string;
  readonly title: string;
  readonly summary: string;
  readonly icon: string;
  readonly tone: 'go' | 'calm' | 'recover' | 'scale';
  readonly steps: readonly string[];
  readonly route: string;
  readonly routeLabel: string;
  readonly manualAnchor: string;
  readonly featured: boolean;
}

const QUICK_PROCEDURES: readonly QuickProcedure[] = [
  {
    id: 'start',
    eyebrow: 'Every session',
    title: 'Create and start a bot',
    summary: 'Create or select a bot → roll call → start → verify.',
    icon: 'pi pi-play',
    tone: 'go',
    steps: [
      'Confirm the account is CLEAN and flat, the fleet is clean, and no freeze is active.',
      'If the bot is new, deploy it without starting. Deployment does not count toward the start-rate ceiling.',
      'Open Bots and run Roll call. The bot needs a fresh, single-use offer for this session.',
      'Open the Ready bot and start it. Review its evidence before starting another bot.',
      'Verify On duty and confirm effective posture: observe-only is the default; submission must be intentional.',
    ],
    route: '/broker/bots',
    routeLabel: 'Open bots',
    manualAnchor: documentAnchor('5.2 Roll call — the offer gate'),
    featured: true,
  },
  {
    id: 'stop',
    eyebrow: 'Preserve recovery',
    title: 'End a bot safely',
    summary: 'Use End day for a re-offerable close; Stop needs Resume later.',
    icon: 'pi pi-stop-circle',
    tone: 'calm',
    steps: [
      'For a normal session close, open the On duty bot and choose End day now.',
      'Wait for Clerk-owned clean-exit proof: the bot becomes Off duty only after it is flat with no open orders.',
      'Use Stop bot gracefully only for a durable halt request; it writes STOPPED and does not flatten.',
      'Before starting a stopped bot later, choose Resume, run fresh roll call, then start from the new offer.',
      'Do not turn a normal day-end into a crash or emergency flatten; both create a harder recovery path.',
    ],
    route: '/broker/bots',
    routeLabel: 'Open bots',
    manualAnchor: documentAnchor('5.4 End day vs. Stop vs. halt/crash — the fork that decides your morning'),
    featured: false,
  },
  {
    id: 'recover',
    eyebrow: 'When blocked',
    title: 'Recover safely',
    summary: 'Read the evidence before choosing a cure.',
    icon: 'pi pi-shield',
    tone: 'recover',
    steps: [
      'Open Accounts and read the freeze, reconciliation, positions, orders, and owner evidence.',
      'For a freeze, reconcile first. Clear it only from a fresh CLEAN receipt.',
      'If the broker is flat but a retired journal claim remains, use a journal cure from the host.',
      'If the broker holds exposure, prefer an exact operator recovery flatten over an account-wide emergency flatten.',
      'For a crashed bot, verify flat and no open orders, then use Retire & Replace.',
    ],
    route: '/broker/accounts',
    routeLabel: 'Open accounts',
    manualAnchor: documentAnchor('7. Freezes & recovery'),
    featured: false,
  },
  {
    id: 'scale',
    eyebrow: 'Fleet launch',
    title: 'Start several bots',
    summary: 'Deploy together, then start and verify one bot at a time.',
    icon: 'pi pi-users',
    tone: 'scale',
    steps: [
      'Get the account CLEAN and flat, fleet clean, broker connected, and tree clean in scope.',
      'Deploy every bot with start disabled; deployment is not rate-gated.',
      'Run roll call to mint current offers for all eligible bots.',
      'Start one Ready bot, verify its On duty evidence, then run a fresh roll call before the next bot.',
      'After each start, verify the account remains unfrozen before continuing.',
    ],
    route: '/broker/bots',
    routeLabel: 'Open bots',
    manualAnchor: documentAnchor('8. Concurrency recipes'),
    featured: false,
  },
];

@Component({
  selector: 'app-operator-quick-procedures',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [RouterLink],
  templateUrl: './operator-quick-procedures.component.html',
  styleUrl: './operator-quick-procedures.component.scss',
})
export class OperatorQuickProceduresComponent {
  readonly procedures = QUICK_PROCEDURES;
}
