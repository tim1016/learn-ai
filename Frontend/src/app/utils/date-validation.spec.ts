/**
 * Date validation utilities — unit tests.
 */
import {
  getMinAllowedDate,
} from './date-validation';

// ---------------------------------------------------------------------------
// getMinAllowedDate
// ---------------------------------------------------------------------------

describe('getMinAllowedDate', () => {
  it('should return a date 2 years in the past', () => {
    const result = getMinAllowedDate();
    const expected = new Date();
    expected.setFullYear(expected.getFullYear() - 2);
    const expectedStr = expected.toISOString().split('T')[0];
    expect(result).toBe(expectedStr);
  });
});
