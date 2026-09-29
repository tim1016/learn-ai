import { DefaultUrlSerializer } from '@angular/router';
import { describe, expect, it } from 'vitest';

import { withoutRetiredLens } from './retired-lens.guard';

const urls = new DefaultUrlSerializer();

describe('withoutRetiredLens', () => {
  it('drops ?lens= and keeps the path, every other parameter and the fragment', () => {
    const tree = withoutRetiredLens(urls.parse('/brokers/alpaca/clerks/c/accounts/a?lens=operator&view=wall#bots'));

    expect(tree === null ? null : urls.serialize(tree)).toBe('/brokers/alpaca/clerks/c/accounts/a?view=wall#bots');
  });

  it('leaves a URL without ?lens= alone', () => {
    expect(withoutRetiredLens(urls.parse('/brokers/alpaca?view=wall'))).toBeNull();
  });
});
