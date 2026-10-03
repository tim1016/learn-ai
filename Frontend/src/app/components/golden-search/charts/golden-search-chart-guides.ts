import type { HighlightTarget } from './golden-search-chart-spec';

/** Every chart with a guide section; the id is that section's anchor in the Golden Search guide. */
export type GoldenSearchChartId =
  | 'candidate-cards'
  | 'equity-and-fall'
  | 'side-by-side'
  | 'neighbor-tornado'
  | 'cost-stress'
  | 'concentration-curve'
  | 'without-best'
  | 'month-calendar'
  | 'monthly-net'
  | 'trade-timeline'
  | 'trade-histogram'
  | 'hold-time'
  | 'entry-rsi'
  | 'entry-time'
  | 'fold-timeline'
  | 'linked-return'
  | 'train-test-sharpe'
  | 'parameter-drift'
  | 'fold-returns'
  | 'fold-activity'
  | 'search-path'
  | 'knob-moves'
  | 'knob-profiles'
  | 'eligibility-map'
  | 'pair-landscape'
  | 'plan-tiles'
  | 'window-map'
  | 'search-space'
  | 'workload'
  | 'trade-minimums'
  | 'data-coverage'
  | 'final-checks'
  | 'final-comparison'
  | 'final-equity'
  | 'final-months';

export interface WalkthroughStep {
  /** The step as the guide's "How to read it" list words it (the contract spec holds the step counts together). */
  readonly text: string;
  /** What the step lights up on the chart; null when it reads the panel as a whole. */
  readonly target: HighlightTarget | null;
}

export interface ChartGuide {
  /** The panel title, and the guide section's heading. */
  readonly title: string;
  /** The one-line question the panel answers. */
  readonly question: string;
  readonly steps: readonly WalkthroughStep[];
}

export const CHART_GUIDES: Readonly<Record<GoldenSearchChartId, ChartGuide>> = {
  'candidate-cards': {
    title: 'Candidate cards',
    question: 'Which settings are you comparing, and how did each do on the development data?',
    steps: [
      { text: 'Find the selected card. The rest of the page explains that candidate; pick another card to switch.', target: null },
      { text: 'Read its settings line: the value of every knob the search chose.', target: null },
      { text: 'Compare net return, Sharpe and worst fall across the cards. More return with a much deeper fall is not a clear win.', target: null },
      { text: 'Check trades against the minimum. A card below its minimum rests on too few trades to judge.', target: null },
      { text: 'Glance at the small line: the same development return as the equity chart, at a glance.', target: null },
    ],
  },
  'equity-and-fall': {
    title: 'Equity and fall from peak',
    question: 'How did each candidate grow over the development period, and how deep were its falls?',
    steps: [
      { text: 'Read the labels at the right edge to see where each candidate finished.', target: { kind: 'point', group: 'equity', at: 'last' } },
      { text: 'Follow each line from left to right. A steady climb is more convincing than one big jump.', target: { kind: 'series', group: 'equity' } },
      {
        text: 'Drop to the lower part at the same date. A line below 0% there means that candidate is still recovering from a drop.',
        target: { kind: 'series', group: 'fall' },
      },
      { text: 'Find the lowest point of each line in the lower part. That is its deepest fall at a session close.', target: { kind: 'point', group: 'fall', at: 'lowest' } },
      { text: 'Note how long each line stays below 0%. Long stretches below a high are hard to sit through.', target: { kind: 'series', group: 'fall' } },
    ],
  },
  'side-by-side': {
    title: 'Candidates side by side',
    question: 'Which candidate leads on each measure?',
    steps: [
      { text: 'Find the selected candidate: its dots are the larger ones.', target: { kind: 'series', group: 'selected' } },
      { text: 'Read one row at a time. Each measure has its own scale, and further right is better on every row.', target: null },
      {
        text: 'Compare the other dots on the same row. A dot well to the right of the selected one marks a measure where that candidate leads.',
        target: { kind: 'series', group: 'other' },
      },
      { text: 'Weigh the rows together. A lead in return that comes with a deeper fall or fewer stress runs in profit is bought with risk.', target: null },
    ],
  },
  'neighbor-tornado': {
    title: 'Neighbor tornado',
    question: 'Does the result hold when one setting moves one step?',
    steps: [
      { text: 'Start at the dashed line at 0: the candidate’s own net return. Each bar shows how far a one-step move changes it.', target: null },
      { text: 'Read the lighter bars: each knob one step below the candidate’s value.', target: { kind: 'series', group: 'below' } },
      { text: 'Read the darker bars: each knob one step above.', target: { kind: 'series', group: 'above' } },
      { text: 'Look for a knob marked “loses money” and its red bar. A one-step change there turns the result into a loss.', target: null },
      { text: 'Hover a knob to see each step’s value and net return, and why a step has no bar.', target: null },
    ],
  },
  'cost-stress': {
    title: 'Cost stress ladder',
    question: 'Does the result survive harsher costs and fills?',
    steps: [
      { text: 'Start with the top bar: the result at the study’s own costs.', target: { kind: 'series', group: 'base' } },
      { text: 'Each bar below reruns the same settings with one harsher cost or fill.', target: { kind: 'series', group: 'stress' } },
      { text: 'Find the stressed bar with the lowest return. That stress is the one the result is most sensitive to.', target: { kind: 'point', group: 'stress', at: 'lowest' } },
      { text: 'A bar left of 0% marks a stress that turns the result into a loss.', target: null },
    ],
  },
  'concentration-curve': {
    title: 'Profit concentration curve',
    question: 'With the trades counted best first, how fast does the net profit pile up?',
    steps: [
      { text: 'A steep climb at the left means the best few trades carry much of the profit.', target: { kind: 'series', group: 'curve' } },
      { text: 'The marked dot shows the share of net profit the best 5% of trades make.', target: { kind: 'series', group: 'best' } },
      { text: 'The peak is where the winning trades end. Its height is all the winners added together.', target: null },
      { text: 'The fall from the peak back to 100% is all the losing trades added together.', target: { kind: 'series', group: 'curve' } },
    ],
  },
  'without-best': {
    title: 'Without its best',
    question: 'Does the result stay profitable without its best month or its best 5% of trades?',
    steps: [
      { text: 'Start from the top bar: the development net profit with every trade.', target: { kind: 'series', group: 'all' } },
      { text: 'Compare the middle bar with it. The gap is what the best month made.', target: { kind: 'series', group: 'month' } },
      { text: 'Compare the bottom bar with it. The gap is what the best 5% of trades made together.', target: { kind: 'series', group: 'trades' } },
      { text: 'A bar at $0 or less turns red. That is the concern the decision summary reports.', target: null },
    ],
  },
  'month-calendar': {
    title: 'Month calendar',
    question: 'Which months made money and which lost it, year by year?',
    steps: [
      { text: 'Read each row as a year and each column as a month. A month outside the development period has no cell.', target: null },
      { text: 'Find the strongest colours: green months made the most, red months lost the most. Each cell prints its amount with its sign.', target: { kind: 'series', group: 'months' } },
      { text: 'Look along each row for runs of red. Losing months in a row are harder to sit through than the same losses spread out.', target: null },
      { text: 'Compare the same month across years. A month that is good every year may be a pattern; one good year is not.', target: null },
    ],
  },
  'monthly-net': {
    title: 'Monthly net profit',
    question: 'How did the net profit move from month to month?',
    steps: [
      { text: 'Read the bars left to right: each is one month’s net profit, a gain above $0 and a loss below.', target: { kind: 'series', group: 'bars' } },
      { text: 'Look for one bar much taller than the rest. A result that rests on one month is fragile; “Without its best” shows how much.', target: null },
      { text: 'Check whether the losing months cluster together or are spread out.', target: null },
      { text: 'Hover a month for its return and how many trades closed in it.', target: null },
    ],
  },
  'trade-timeline': {
    title: 'Trade timeline',
    question: 'When was the profit made, trade by trade?',
    steps: [
      { text: 'Follow the upper line: the running net profit after each trade, in exit order.', target: { kind: 'series', group: 'running' } },
      { text: 'Find where it rose most. Profit made in a few short stretches is less dependable than a steady climb.', target: null },
      { text: 'Find the line’s lowest point. Below $0 there means the trades had lost money overall by then.', target: { kind: 'point', group: 'running', at: 'lowest' } },
      { text: 'Read the bars beneath: each trade’s own net profit, at its exit.', target: { kind: 'series', group: 'trades' } },
      { text: 'Hover a trade for its full record: times, prices, quantity, P&L before and after commission, bars held, RSI at entry and why it exited.', target: null },
    ],
  },
  'trade-histogram': {
    title: 'Trade net profit histogram',
    question: 'What do the wins and losses look like, trade by trade?',
    steps: [
      { text: 'Each bar counts the trades whose net profit falls in its range. Bars left of $0 are losing trades.', target: { kind: 'series', group: 'losses' } },
      { text: 'Read the winning side: are the wins many and small, or few and large?', target: { kind: 'series', group: 'wins' } },
      { text: 'Compare the tails. A long tail on the losing side means a few trades lost far more than usual.', target: null },
      { text: 'Hover a bar for its range and how many trades fell in it.', target: null },
    ],
  },
  'hold-time': {
    title: 'Hold time against net profit',
    question: 'Does holding a trade longer help or hurt?',
    steps: [
      { text: 'Each dot is a trade: the decision bars it was held across, and its net profit.', target: null },
      { text: 'Read the circles: trades the strategy closed itself. A strategy that exits on a timer holds most of its trades for the same number of bars.', target: { kind: 'series', group: 'strategy' } },
      { text: 'Find any diamonds: trades closed because the tested window ended, not by the strategy.', target: { kind: 'series', group: 'window' } },
      { text: 'Compare the spread above and below $0 at each hold. If longer holds lose more often, holding longer is not helping.', target: null },
    ],
  },
  'entry-rsi': {
    title: 'RSI at entry against net profit',
    question: 'Does a better RSI band exist inside the gates?',
    steps: [
      { text: 'Each dot is a trade: the RSI the strategy saw when it decided to enter, and the trade’s net profit.', target: { kind: 'series', group: 'trades' } },
      { text: 'The dashed lines are the RSI gates. Every entry falls between them.', target: null },
      { text: 'Read the level lines: the average net profit of the trades in each 5-point band.', target: { kind: 'series', group: 'bands' } },
      { text: 'Look for a band whose average is clearly better or worse than the rest, and check its trade count before trusting it.', target: null },
    ],
  },
  'entry-time': {
    title: 'Entry time and weekday',
    question: 'Do trades entered at some times of day or on some weekdays do better?',
    steps: [
      { text: 'Each cell is a weekday and an Eastern half hour; its number is how many trades entered then.', target: null },
      { text: 'Read the coloured cells: green where those trades made money on average, red where they lost.', target: { kind: 'series', group: 'enough' } },
      { text: 'Grey cells hold too few trades to judge. Don’t read a pattern into them.', target: { kind: 'series', group: 'few' } },
      { text: 'Look for a row or column that is red or green across several cells. A real pattern shows in more than one cell.', target: null },
    ],
  },
  'fold-timeline': {
    title: 'Fold timeline',
    question: 'Which months did each fold train on and test on, and how did each end?',
    steps: [
      { text: 'Read each row as one fold: the grey bar is its training window, the coloured bar the test window right after it.', target: { kind: 'series', group: 'training' } },
      { text: 'The test windows follow one another in time, so together they cover a stretch the procedure never trained on before choosing.', target: { kind: 'series', group: 'test' } },
      { text: 'A green test bar made money on its test window and a red one lost; an outlined bar is a fold that failed, and a grey one has not run yet.', target: null },
      { text: 'Hover a fold for its windows, both Sharpes, its retention, test return and trades.', target: null },
    ],
  },
  'linked-return': {
    title: 'Linked test return',
    question: 'If the search procedure had chosen fresh settings before each test window, how would its test results have added up?',
    steps: [
      { text: 'Follow the solid line: each fold’s test return, linked one after another.', target: { kind: 'series', group: 'procedure' } },
      { text: 'Compare the dashed line: the current settings on the same test windows.', target: { kind: 'series', group: 'incumbent' } },
      { text: 'A gap in a line is a missing fold. Everything after it is cut off, because linking needs every fold.', target: null },
      { text: 'Remember what it judges: the procedure that chose the settings, not any one candidate.', target: null },
    ],
  },
  'train-test-sharpe': {
    title: 'Training against test Sharpe',
    question: 'How much of each fold’s training Sharpe survived on its test window?',
    steps: [
      { text: 'Each fold has two bars: the winner’s Sharpe on its training window, then on its test window.', target: { kind: 'series', group: 'training' } },
      { text: 'Read the label above each test bar: the share of the training Sharpe it kept, its retention.', target: { kind: 'series', group: 'test' } },
      { text: 'The verdict takes the median retention over folds and asks for 50% or more.', target: null },
      { text: 'A fold with no label has no defined retention: it failed, or its training Sharpe was not above 0.', target: null },
    ],
  },
  'parameter-drift': {
    title: 'Parameter drift',
    question: 'Did the search choose similar settings in every fold?',
    steps: [
      { text: 'Each row is one searched knob on its own range; the dots are the value each fold’s training chose.', target: { kind: 'series', group: 'winners' } },
      { text: 'The dashed line is the all-period winner and the dotted line the current settings.', target: null },
      { text: 'Dots that stay close together mean the search found the same answer each time.', target: null },
      { text: 'Dots that jump around, or sit at the edge of the range, mean the best value depends on the period, or lies outside what was searched.', target: null },
    ],
  },
  'fold-returns': {
    title: 'Test return per fold',
    question: 'Fold by fold, did the procedure’s winner beat the current settings on the same test window?',
    steps: [
      { text: 'Each fold has two bars: the return of the winner its training chose, and the current settings on the same test window.', target: { kind: 'series', group: 'procedure' } },
      { text: 'Compare them fold by fold. The grey bar is the benchmark.', target: { kind: 'series', group: 'incumbent' } },
      { text: 'Count the folds where the procedure leads. Winning a few folds by a lot is weaker than winning most of them.', target: null },
      { text: 'A fold with only a grey bar is one whose winner failed; hover it for what the current settings did.', target: null },
    ],
  },
  'fold-activity': {
    title: 'Test activity per fold',
    question: 'Did the folds trade enough on their test windows to judge?',
    steps: [
      { text: 'Each bar on the left is one fold’s test trades.', target: { kind: 'series', group: 'folds' } },
      { text: 'The bar on the right is all completed folds together.', target: { kind: 'series', group: 'total' } },
      { text: 'The dashed line is the forward minimum: the trades all forward tests must reach together. It applies to the total, not to each fold.', target: null },
      { text: 'A total below the line turns red: too few test trades to judge the procedure.', target: null },
    ],
  },
  'search-path': {
    title: 'Search path',
    question: 'How did the search reach its winner, point by point?',
    steps: [
      { text: 'Each dot is a point the search scored, in the order it tried them; the first is the starting point.', target: { kind: 'series', group: 'eligible' } },
      { text: 'Grey dots fail a rule: too few trades, too deep a fall or no profit. They can never win.', target: { kind: 'series', group: 'ineligible' } },
      { text: 'The line is the best result so far that meets the rules. It only rises when a knob moves to a better value.', target: { kind: 'series', group: 'best' } },
      { text: 'A line that flattens early means later rounds found nothing better along the moves they tried — not that nothing better exists.', target: null },
    ],
  },
  'knob-moves': {
    title: 'Knob moves',
    question: 'Where did each searched knob start, and where did it end within its range?',
    steps: [
      { text: 'Each row is a searched knob, its range drawn from low to high.', target: null },
      { text: 'The hollow dot is where the knob started.', target: { kind: 'series', group: 'start' } },
      { text: 'The filled dot is the value the search kept, labelled with that value.', target: { kind: 'series', group: 'retained' } },
      { text: 'An amber dot sits at an end of its range: a better value may lie outside what was searched.', target: null },
    ],
  },
  'knob-profiles': {
    title: 'One-knob profiles',
    question: 'How did the result change as each knob moved on its own?',
    steps: [
      { text: 'Each small chart is one knob: its result at every value tried, every other knob held.', target: { kind: 'series', group: 'profile' } },
      { text: 'The blue dot is the value kept. Hollow dots fail a rule.', target: null },
      { text: 'A smooth rise and fall around the kept value is reassuring; a lone spike next to poor neighbours is not.', target: null },
      { text: 'Hover a dot to see what the other knobs were held at: for Zoom, their values when this knob was searched, not the final winner.', target: null },
    ],
  },
  'eligibility-map': {
    title: 'Eligibility map',
    question: 'Which of the points scored meet the rules, and what stops the rest?',
    steps: [
      { text: 'Each dot is a point scored on this window: across, its trades; up, its net profit.', target: null },
      { text: 'Blue dots meet the rules. The outlined one is the winner.', target: { kind: 'series', group: 'eligible' } },
      { text: 'The other colours name the rule a point fails: too few trades, too deep a fall, no profit, or a run that failed.', target: { kind: 'series', group: 'trades' } },
      { text: 'The dashed lines are the trade floor and $0. Many points just past them mean the rules shaped the choice.', target: null },
    ],
  },
  'pair-landscape': {
    title: 'Pair landscape',
    question: 'Does the result survive nearby settings of two knobs together?',
    steps: [
      { text: 'Each cell is one pair of values: rows for one knob, columns for the other, every other setting held at the candidate.', target: { kind: 'series', group: 'tested' } },
      { text: 'Green cells made money and red cells lost it; each cell prints its net return.', target: null },
      { text: 'The outlined cell is the candidate itself. A plateau of similar cells around it is more robust than a lone peak.', target: null },
      { text: 'Grey cells have no return: an invalid pair (—), outside the legal range (·), never run (?), a failed run, or one that recorded none.', target: { kind: 'series', group: 'other' } },
    ],
  },
  'plan-tiles': {
    title: 'Plan at a glance',
    question: 'What did you lock in, in four numbers?',
    steps: [
      { text: 'The search method: Zoom moves one knob at a time; Grid tries every combination.', target: null },
      { text: 'The development period: the data the search chooses on, with its trading sessions.', target: null },
      { text: 'The final test: kept sealed until you choose, then opened once.', target: null },
      { text: 'The engine runs the plan may use, against the study’s cap.', target: null },
    ],
  },
  'window-map': {
    title: 'Window map',
    question: 'Which months does each part of the study use?',
    steps: [
      { text: 'Each row is a window, on one time axis: the run-up, development, the recent fit, each fold, and the final test.', target: { kind: 'series', group: 'windows' } },
      { text: 'The run-up only warms the indicators; nothing is judged on it.', target: null },
      { text: 'The folds’ test windows follow one another; together they are the forward tests the verdict counts trades over.', target: null },
      { text: 'Hover a window for its dates, sessions and trade minimum. The final test sits after everything else, untouched.', target: null },
    ],
  },
  'search-space': {
    title: 'Search space',
    question: 'How much of each knob’s legal range does the search explore?',
    steps: [
      { text: 'Each row is a knob, drawn across its whole legal range.', target: null },
      { text: 'The band is the range the search may try; a held knob is a single grey mark.', target: { kind: 'series', group: 'range' } },
      { text: 'The diamond is the current settings; the ring is where Zoom starts. Grid tries only the band. Zoom weighs the value it holds in every round, so it can end where it started even outside the band.', target: { kind: 'series', group: 'current' } },
      { text: 'Hover a knob for its step, how many values it can take and its importance: higher importance is searched first.', target: null },
    ],
  },
  workload: {
    title: 'Workload',
    question: 'How many engine runs does each stage plan, and how many has it used?',
    steps: [
      { text: 'Each row is a stage of the study.', target: null },
      { text: 'The grey bar is the most runs the plan allows the stage.', target: { kind: 'series', group: 'planned' } },
      { text: 'The blue bar is how many it has used so far, runs in flight included.', target: { kind: 'series', group: 'used' } },
      { text: 'The plan is an upper bound, not a stop. A stage stops short only when the study’s cap runs out; the study then notes it as incomplete.', target: null },
    ],
  },
  'trade-minimums': {
    title: 'Trade minimums',
    question: 'How many trades must each window reach for its results to count?',
    steps: [
      { text: 'Each bar is a window’s trade minimum.', target: { kind: 'series', group: 'minimum' } },
      { text: 'With an expected trade frequency, the minimum grows with the window’s trading years, rounded up.', target: null },
      { text: 'Hover a window for its trading years, year by year.', target: null },
      { text: 'A fixed-floor plan shows its two floors instead: one for every selection window, one for the final test.', target: null },
    ],
  },
  'data-coverage': {
    title: 'Data coverage',
    question: 'Does the lake hold the minute bars the study’s data span needs?',
    steps: [
      { text: 'Each bar is a month of the study’s data span, as tall as its trading sessions.', target: null },
      { text: 'Green sessions are complete in the lake.', target: { kind: 'series', group: 'complete' } },
      { text: 'Amber is still fetching, teal stale, red failed, and grey not in the lake at all.', target: { kind: 'series', group: 'missing' } },
      { text: 'This is the lake now. The study ran on the data snapshot frozen at lock, which this cannot change.', target: null },
    ],
  },
  'final-checks': {
    title: 'Final-test checks',
    question: 'Did the final test meet each rule, in words?',
    steps: [
      { text: 'Each line is one rule the final test is judged by, with its outcome first.', target: null },
      { text: 'A pass meets the rule; a fail does not; “not available” means the run could not say.', target: null },
      { text: 'Read the detail after the dash: the numbers the rule compared.', target: null },
      { text: 'Retention is described beside the result, never checked: a big drop with every rule passed is still worth a second look.', target: null },
    ],
  },
  'final-comparison': {
    title: 'Development against final',
    question: 'Which measures held up on the final test?',
    steps: [
      { text: 'Each block is one measure on its own scale: annualized return, Sharpe, worst fall, trades a year.', target: null },
      { text: 'The teal bar is the development period: the data the study chose from.', target: { kind: 'series', group: 'development' } },
      { text: 'The blue bar is the final test: data no step chose on.', target: { kind: 'series', group: 'final' } },
      { text: 'Compare the candidate’s change with the current settings’: a drop both share is the market; a drop only the candidate shows is the fit.', target: null },
    ],
  },
  'final-equity': {
    title: 'Final-test equity',
    question: 'How did the candidate and the current settings do through the final test?',
    steps: [
      { text: 'Read the labels at the right edge: where each run ended the final test.', target: { kind: 'point', group: 'equity', at: 'last' } },
      { text: 'Follow each line: a steady climb is more convincing than one jump.', target: { kind: 'series', group: 'equity' } },
      { text: 'Drop to the lower part and find its lowest point: the deepest fall from a peak at a session close.', target: { kind: 'point', group: 'fall', at: 'lowest' } },
      { text: 'The amber dashed line labelled “limit” is the worst-fall limit. The final test reads the candidate’s fall bar by bar, which can be deeper than its line here.', target: null },
    ],
  },
  'final-months': {
    title: 'Final-test months',
    question: 'Was the final result spread out, or carried by one month?',
    steps: [
      { text: 'Each pair of bars is one month of the final test: the candidate and the current settings.', target: { kind: 'series', group: 'months' } },
      { text: 'Look for one month far taller than the rest: a final result carried by a single month is fragile.', target: null },
      { text: 'Count the months each run won. Winning most months is steadier than winning one big one.', target: null },
      { text: 'Hover a month for both returns.', target: null },
    ],
  },
};
