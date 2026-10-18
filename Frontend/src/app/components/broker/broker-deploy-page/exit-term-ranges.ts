/** Each exit term's accepted range, as the backend's `ExitTermsInput` holds it. */
export const EXIT_TERM_RANGES = {
  exitAllowanceBps: { min: 0, max: 9999.999, message: 'Exit allowance must be between 0 and 9,999.999 bps.' },
  bandMultiple: { min: 1, max: 10, message: 'Band multiple must be between 1 and 10×.' },
  spreadCapBps: { min: 1, max: 1000, message: 'Spread cap must be between 1 and 1,000 bps.' },
} as const;
