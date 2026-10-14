export const MANUAL_STEPS = [
  {
    id: 'plan', title: 'Plan', question: 'What would convince you before you see the answer?',
    action: 'Choose the declared strategy and instrument. Set the search ranges, training and test months, final-test length, trade activity, drawdown limit, fills and costs. Read the problems listed under the plan, fix every refusal, then Run research. Lock only saves the plan without starting it.',
    read: 'Use the window map to check the actual dates. The calendar may leave months out to fit whole folds. Check coverage and the final-test exposure label; previously used dates make the result exploratory.',
    stop: 'Freeze the rules before searching. A minimum trade count is an activity floor, not a statistical confidence guarantee. Do not lower the floor after seeing a poor result.',
    links: [{label: 'Window map', anchor: 'window-map'}, {label: 'Data coverage', anchor: 'data-coverage'}, {label: 'Trade minimums', anchor: 'trade-minimums'}],
  },
  {
    id: 'search', title: 'Search', question: 'Did the search find a stable region or one lucky setting?',
    action: 'Let the server finish. You may leave the page and return through History. Search shows the winning settings and why each knob stopped. All runs opens the underlying engine results and curves.',
    read: 'Follow the search path, then the one-knob profiles and eligibility map. Zoom searches one knob at a time; pair audits reveal interactions that it can miss. An isolated spike needs more scrutiny than a broad neighborhood.',
    stop: 'Development data chose these settings. Its performance is optimistic. A Zoom winner is not a proven global optimum. A cancelled or budget-limited study has incomplete evidence.',
    links: [{label: 'Search path', anchor: 'search-path'}, {label: 'One-knob profiles', anchor: 'knob-profiles'}, {label: 'Eligibility map', anchor: 'eligibility-map'}],
  },
  {
    id: 'test', title: 'Test over time', question: 'Does the search procedure survive later market periods?',
    action: 'Run research continues here automatically and stops at Compare. Each fold searches only its training months, then tests those settings on its following test months. Compare the fold winners with the frozen incumbent.',
    read: 'Start with the fold timeline and linked returns. Then check training against test Sharpe, parameter drift and trade activity. A missing fold breaks the linked curve; it is not a flat return.',
    stop: 'This tests the search procedure, not the fixed all-period fit. The retained legacy verdict is a heuristic, not a probability of future profit. Do not join separate fresh-capital curves into a live account history.',
    links: [{label: 'Fold timeline', anchor: 'fold-timeline'}, {label: 'Training versus test', anchor: 'train-test-sharpe'}, {label: 'Parameter drift', anchor: 'parameter-drift'}],
  },
  {
    id: 'compare', title: 'Compare', question: 'Is there enough evidence to spend the final look?',
    action: 'Select each candidate card: all-period fit, recent fit and current settings. Review equity, neighbors, pair maps, cost stresses and concentration. Read the decision summary, then the three chance panels. Keep current settings is a valid decision.',
    read: 'The difference band asks whether the fit beats the incumbent under resampling. Zero is a tie. Rehearsal asks how often the frozen rules survive different test lengths. Drawdown asks how severe session-close falls may be under the model.',
    stop: '10,000 resamples are not 10,000 independent market histories. They reshuffle recorded session returns, do not rerun fills, and do not correct for choosing the winner. A high support label is not a chance of future success; missing readings require attention.',
    links: [{label: 'Equity and fall', anchor: 'equity-and-fall'}, {label: 'Cost stresses', anchor: 'cost-stress'}, {label: 'Chance comparison', anchor: 'chance-comparison'}, {label: 'Rehearsal', anchor: 'chance-rehearsal'}, {label: 'Drawdown uncertainty', anchor: 'chance-drawdown'}],
  },
  {
    id: 'decision', title: 'Final test and decision', question: 'What survived the one final look?',
    action: 'Open Final decision, read the sealed dates and exposure, and open the final test once. Every fit and the frozen incumbent run. Review every fit before making an approval decision. Revise creates a linked new study rather than editing the original.',
    read: 'Keep the frozen rule checks separate from resampling support. Review final equity, monthly results and development versus final. The rehearsal rank compares the recorded final objective with development rehearsals; an unusually high rank can also reflect a lucky period.',
    stop: 'Selecting the better fit after viewing both outcomes introduces selection bias. A positive Sharpe is not proof of a positive compounded return. Reusing the same final dates is exploratory. Approval changes research defaults and starts qualification; it does not itself deploy a bot.',
    links: [{label: 'Final checks', anchor: 'final-checks'}, {label: 'Development versus final', anchor: 'final-comparison'}, {label: 'Final uncertainty', anchor: 'chance-final'}],
  },
] as const;
