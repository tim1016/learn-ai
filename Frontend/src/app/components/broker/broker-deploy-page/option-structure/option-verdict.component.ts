import { ChangeDetectionStrategy, Component, computed, input } from '@angular/core';

import type { components } from '../../../../api/broker.types';

type OptionPreviewVerdict = components['schemas']['OptionPreviewVerdict'];

/** One half of the verdict: the server's yes or no, and its sentence. */
interface VerdictHalf {
  readonly mark: string;
  /** The mark in a word, for a reader who cannot see it. */
  readonly spoken: string;
  readonly sentence: string;
}

const YES = { mark: '✓', spoken: 'Yes' } as const;
const NO = { mark: '✕', spoken: 'No' } as const;
const UNKNOWN = { mark: '?', spoken: 'Not known' } as const;

/**
 * The structure builder's verdict line (#2915): always there, and in the
 * server's own sentences. Its two halves are whether the structure can be
 * deployed at all, and whether what it resolves to right now is what was
 * built. Red is the server's word for it (`red`), never worked out here. It
 * is green only when both halves are a yes: a half the server does not know
 * yet leaves it plain. Each half carries a mark and a sentence, so colour is
 * never the only sign.
 */
@Component({
  selector: 'app-option-verdict',
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './option-verdict.component.html',
  styleUrl: './option-verdict.component.scss',
  host: {
    role: 'status',
    '[class.option-verdict--red]': 'verdict()?.red === true',
    '[class.option-verdict--green]': 'verdict()?.can_be_deployed === true && verdict()?.resolves_as_built === true',
  },
})
export class OptionVerdictComponent {
  /** The preview's verdict; `null` while the preview has no answer. */
  readonly verdict = input<OptionPreviewVerdict | null>(null);

  protected readonly halves = computed<readonly VerdictHalf[] | null>(() => {
    const told = this.verdict();
    if (told === null) return null;
    const resolves = told.resolves_as_built === null ? UNKNOWN : told.resolves_as_built ? YES : NO;
    return [
      { ...(told.can_be_deployed ? YES : NO), sentence: told.deploy_sentence },
      { ...resolves, sentence: told.resolves_sentence },
    ];
  });
}
