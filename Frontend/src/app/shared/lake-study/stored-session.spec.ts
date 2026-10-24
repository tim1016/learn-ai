import { describe, expect, it } from 'vitest';

import { StoredInputs, StoredInputsError } from '../../research/stored-study/stored-inputs';
import { storedSession } from './stored-session';

describe('storedSession', () => {
  it('reads the session a saved run names, whatever the fallback is', () => {
    expect(storedSession(new StoredInputs({ session: 'rth' }), 'extended')).toBe('rth');
    expect(storedSession(new StoredInputs({ session: 'extended' }), 'rth')).toBe('extended');
  });

  it('gives the fallback when the run names no session, which is the session its study computed on before it named one', () => {
    expect(storedSession(new StoredInputs({}), 'extended')).toBe('extended');
    expect(storedSession(new StoredInputs({ session: null }), 'rth')).toBe('rth');
  });

  it('refuses a name that is not a session, and a value that is not text, rather than defaulting', () => {
    expect(() => storedSession(new StoredInputs({ session: 'overnight' }), 'rth')).toThrow(StoredInputsError);
    expect(() => storedSession(new StoredInputs({ session: 5 }), 'rth')).toThrow(StoredInputsError);
  });
});
