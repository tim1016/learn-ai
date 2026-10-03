# Reading the Golden Search charts

This guide explains the charts on the Golden Search pages. For each chart it covers the question the chart answers, what is drawn, how to read it, the good signs and the warning signs, and an everyday comparison.

## How to use this guide

Every chart panel has two buttons:

- **About this chart** opens this guide at that chart's section, in a drawer beside the live chart, so you can read and look at the same time.
- **Walk me through it** steps through the chart's "How to read it" list on the chart itself. Each step lights up the part of the chart it talks about. Use Back and Next, or the arrow keys; Esc leaves the walkthrough.

A few rules hold for every chart:

- **Hover for the numbers.** Hovering any mark shows what it is, its value with units, and what to judge it against: the rule it is measured against, the candidate it is compared with, or how many trades it rests on.
- **Missing is never zero.** A value the study did not record reads "not recorded". It is never drawn as zero, and it never counts as a pass.
- **Every number comes from the study.** The charts draw values the research recorded; the page computes none of its own.
- **Show as table.** Every chart can show its values as a table, for reading without a mouse or with a screen reader.

## Plan

The Plan step shows what a locked study froze: its windows, its search space, how much work it may do, how many trades each window needs, and whether the data lake holds the bars it reads. Nothing here changes after lock; Revise starts a new study.

### Plan at a glance {#plan-tiles}

#### The question it answers

What did you lock in, in four numbers?

#### What you're looking at

Four tiles: the search method and its settings, the development period with its trading sessions, the final test with its sessions and whether it has been opened, and the engine runs the plan may use against the study's cap.

#### How to read it

1. The search method: Zoom moves one knob at a time; Grid tries every combination.
2. The development period: the data the search chooses on, with its trading sessions.
3. The final test: kept sealed until you choose, then opened once.
4. The engine runs the plan may use, against the study’s cap.

#### Good signs and warning signs

- Good: a development period of several years and a final test long enough to hold real trades.
- Warning: a short final test. Opened once, it may hold too few trades to tell anything.

#### An everyday comparison

The cover sheet of an exam: how long it is, what it covers, and how much time you have.

### Window map {#window-map}

#### The question it answers

Which months does each part of the study use?

#### What you're looking at

A row for each window the plan evaluates, on one time axis in Eastern dates: the run-up that warms the indicators up, the development period, the recent fit's window, each fold's training and test windows, all forward tests together, and the final test. Hover a window for its dates, its trading sessions and its trade minimum. The run-up and a single fold's test have no minimum of their own.

$$\text{sessions in a window} = \text{the trading calendar's sessions from its first to its last day}$$

#### How to read it

1. Each row is a window, on one time axis: the run-up, development, the recent fit, each fold, and the final test.
2. The run-up only warms the indicators; nothing is judged on it.
3. The folds’ test windows follow one another; together they are the forward tests the verdict counts trades over.
4. Hover a window for its dates, sessions and trade minimum. The final test sits after everything else, untouched.

#### Good signs and warning signs

- Good: the final test entirely after the development period, with no overlap.
- Warning: folds whose training windows are only a month or two. Each fold then chooses on very little data.

#### An everyday comparison

A school year planner: term dates, practice tests along the way, and the final exam at the end.

### Search space {#search-space}

#### The question it answers

How much of each knob's legal range does the search explore?

#### What you're looking at

A row for each knob, drawn across its whole legal range, in the order the search takes them. The blue band is the range the search may try; a held knob is a single grey mark at its value. The diamond is the current settings. On a Zoom plan, the ring is where Zoom starts: the plan's seed, which is the current settings unless the plan names another. Hover a knob for its step, how many values it can take, and its importance.

$$\text{position} = \frac{\text{value} - \text{legal low}}{\text{legal high} - \text{legal low}}$$

#### How to read it

1. Each row is a knob, drawn across its whole legal range.
2. The band is the range the search may try; a held knob is a single grey mark.
3. The diamond is the current settings; the ring is where Zoom starts. Grid tries only the band. Zoom keeps its starting value in every round, so it can end there even outside the band.
4. Hover a knob for its step, how many values it can take and its importance: higher importance is searched first.

#### Good signs and warning signs

- Good: bands that include the current settings, so the search weighs today's values against the alternatives.
- Warning: a narrow band far from the current settings. Grid can only move away; Zoom moves into the band only when a value there does strictly better than where it starts.

#### An everyday comparison

A map with the area you will search shaded in, and a pin where you are standing now.

### Workload {#workload}

#### The question it answers

How many engine runs does each stage plan, and how many has it used?

#### What you're looking at

A row for each stage. The grey bar is the most engine runs the stage could need, frozen at lock: an upper bound, not a stop. The blue bar is how many it has used so far, runs still in flight included; the search's include its pair audits, and together the bars add up to the runs the study has used. A cached answer reuses an earlier run without spending the cap.

#### How to read it

1. Each row is a stage of the study.
2. The grey bar is the most runs the plan allows the stage.
3. The blue bar is how many it has used so far, runs in flight included.
4. The plan is an upper bound, not a stop. A stage stops short only when the study’s cap runs out; the study then notes it as incomplete.

#### Good signs and warning signs

- Good: blue bars inside their grey bars, and runs used well under the study's cap.
- Warning: runs used close to the cap. A stage that runs out of the cap stops short, and its results are incomplete.

#### An everyday comparison

A budget beside the receipts: what each line was allowed, and what it has spent.

### Trade minimums {#trade-minimums}

#### The question it answers

How many trades must each window reach for its results to count?

#### What you're looking at

A bar for each window's trade minimum. With an expected trade frequency, each window's minimum is that frequency times the window's trading years, rounded up, where a trading year counts each calendar year's sessions in the window against all its sessions:

$$\text{minimum} = \left\lceil \text{trades a year} \times \sum_{\text{years}} \frac{\text{sessions in the window that year}}{\text{sessions that year}} \right\rceil$$

The receipt froze these at lock. A fixed-floor plan shows its two floors instead. Hover a window for its trading years, year by year.

#### How to read it

1. Each bar is a window’s trade minimum.
2. With an expected trade frequency, the minimum grows with the window’s trading years, rounded up.
3. Hover a window for its trading years, year by year.
4. A fixed-floor plan shows its two floors instead: one for every selection window, one for the final test.

#### Good signs and warning signs

- Good: minimums that match how often the strategy really trades.
- Warning: minimums far above what the strategy trades. Every window will fail the rule.

#### An everyday comparison

A survey's minimum sample: a longer survey needs more answers before its result counts.

### Data coverage {#data-coverage}

#### The question it answers

Does the lake hold the minute bars the study's data span needs?

#### What you're looking at

A bar for each month of the study's data span, from the run-up's start to the final test's end, as tall as the month's trading sessions. Each bar is split by what the data lake holds for those sessions: complete in green, still fetching in amber, stale (held but due to be fetched again) in teal, failed in red, and not in the lake at all in grey. Weekends and holidays are not sessions, so they never count as missing.

This is the lake now. The study ran on the data snapshot frozen at lock, which this cannot change.

#### How to read it

1. Each bar is a month of the study’s data span, as tall as its trading sessions.
2. Green sessions are complete in the lake.
3. Amber is still fetching, teal stale, red failed, and grey not in the lake at all.
4. This is the lake now. The study ran on the data snapshot frozen at lock, which this cannot change.

#### Good signs and warning signs

- Good: solid green bars.
- Warning: grey or red months inside the span. A revised plan reading them would find holes.

#### An everyday comparison

A library shelf check: every volume of a series on the shelf, or gaps where some are out or lost.

## Search

Search fits the settings. The all-period search runs on the whole development period from the frozen starting point; when the plan asks for it, the recent fit runs the same way on only the last few months. Every number here is in-sample: these runs chose the settings, so they show how the choice was made, not how well it will hold.

### Search path {#search-path}

#### The question it answers

How did the search reach its winner, point by point?

#### What you're looking at

Zoom improves the settings one knob at a time. Each dot is a point it scored, left to right in the order it tried them; the first is the starting point. Up is the plan's objective (Sharpe, unless the plan chose another). Blue dots meet the rules; grey dots fail one and can never win.

The line is the best objective so far among the points that meet the rules:

$$\text{best so far after point } k = \max \{ \text{objective of point } i : i \le k, \ i \text{ meets the rules} \}$$

The chart rebuilds the path from the procedure's own record. If the rebuilt path does not end at the recorded winner, the panel says so and draws nothing. Grid scores every combination at once, so a Grid study has no path.

Hover a dot for its step (pass, knob and round), the value tried, its full settings, and its Sharpe, return, profit, trades and worst fall.

#### How to read it

1. Each dot is a point the search scored, in the order it tried them; the first is the starting point.
2. Grey dots fail a rule: too few trades, too deep a fall or no profit. They can never win.
3. The line is the best result so far that meets the rules. It only rises when a knob moves to a better value.
4. A line that flattens early means later rounds found nothing better along the moves they tried — not that nothing better exists.

#### Good signs and warning signs

- Good: the line climbs in a few steps and many nearby dots score close to it.
- Warning: the line jumps once to a value no other dot comes near. One lucky point carried the choice.
- Warning: most dots grey. The rules rejected most of what was tried.

#### An everyday comparison

Tuning a radio one knob at a time: turn the frequency until the signal peaks, then the volume. You find a good setting, but not necessarily the best one a different order would have found.

### Knob moves {#knob-moves}

#### The question it answers

Where did each searched knob start, and where did it end within its range?

#### What you're looking at

A row for each searched knob, its searched range drawn from its low end to its high end. The hollow dot is the starting value; the filled dot is the value the search kept, labelled with its value. A kept value at either end of its range is amber: the best value may lie outside what was searched.

$$\text{position} = \frac{\text{value} - \text{low}}{\text{high} - \text{low}}$$

Hover a row for the range and both values.

#### How to read it

1. Each row is a searched knob, its range drawn from low to high.
2. The hollow dot is where the knob started.
3. The filled dot is the value the search kept, labelled with that value.
4. An amber dot sits at an end of its range: a better value may lie outside what was searched.

#### Good signs and warning signs

- Good: kept values well inside their ranges.
- Warning: a kept value at an edge. Widen the range in a new plan before trusting it.
- Warning: every knob far from where it started. The result depends on a big change from settings you know.

#### An everyday comparison

Adjusting a car seat: if you end up with the seat pushed all the way back, the rail is too short, and a longer one might fit better still.

### One-knob profiles {#knob-profiles}

#### The question it answers

How did the result change as each knob moved on its own?

#### What you're looking at

A small chart for each searched knob: its objective at every value tried, every other knob held. The blue dot is the value kept; hollow dots fail a rule.

For Zoom, each profile is the knob's last pass, and the other knobs are held at the values they had when this knob was searched — not at the final winner, since later knobs may have moved after. For Grid, each profile is a slice of the grid through the winner: every scored point that matches the winner on every other knob.

Hover a dot for its numbers and the values the other knobs were held at.

#### How to read it

1. Each small chart is one knob: its result at every value tried, every other knob held.
2. The blue dot is the value kept. Hollow dots fail a rule.
3. A smooth rise and fall around the kept value is reassuring; a lone spike next to poor neighbours is not.
4. Hover a dot to see what the other knobs were held at: for Zoom, their values when this knob was searched, not the final winner.

#### Good signs and warning signs

- Good: a broad hill with the kept value near its top.
- Warning: a narrow spike. A small change from the kept value gives a much worse result.
- Warning: a flat line. The knob barely matters, and the kept value is close to arbitrary.

#### An everyday comparison

Seasoning a soup one spice at a time while the rest stay fixed. If the taste swings wildly with a pinch more or less, the recipe is fragile.

### Eligibility map {#eligibility-map}

#### The question it answers

Which of the points scored meet the rules, and what stops the rest?

#### What you're looking at

Every point scored on the window, with its trades across and its net profit up. The colour names the rule a point fails: blue meets the rules, amber has too few trades, red falls too deep, light blue makes no profit, and grey failed or has no objective or worst fall recorded. A failed run has no numbers, so it appears only in the table. The outlined dot is the winner. The dashed lines are the window's trade floor and, when the plan requires a profit, $0.

The rules are the plan's frozen ones, with this window's trade floor. Hover a dot for its settings and numbers.

#### How to read it

1. Each dot is a point scored on this window: across, its trades; up, its net profit.
2. Blue dots meet the rules. The outlined one is the winner.
3. The other colours name the rule a point fails: too few trades, too deep a fall, no profit, or a run that failed.
4. The dashed lines are the trade floor and $0. Many points just past them mean the rules shaped the choice.

#### Good signs and warning signs

- Good: plenty of blue dots, the winner among many similar ones.
- Warning: the winner alone in its corner. Few settings behave like it.
- Warning: a crowd of dots just below the trade floor. The floor, not the strategy, decided much of the search.

#### An everyday comparison

Job applicants plotted by experience and test score, with the hiring bar drawn in. A hire surrounded by many strong applicants is a safer choice than one standing far apart.

### Pair landscape {#pair-landscape}

#### The question it answers

Does the result survive nearby settings of two knobs together?

#### What you're looking at

A grid of cells, one per pair of values: rows for one knob, columns for the other, every other setting held at the candidate's. Each cell prints the development net return and is coloured by it: green for a gain, red for a loss, with 0% in the middle. The outlined cell is the candidate itself. Grey cells have no return: an invalid pair (—), a value outside the legal range (·), a pair never run (?), a run that failed (Failed), or one that recorded no return (no return). When the plan audited more than one pair, buttons above the chart switch between them.

The landscape is a two-knob slice: it says nothing about combinations of the other knobs.

#### How to read it

1. Each cell is one pair of values: rows for one knob, columns for the other, every other setting held at the candidate.
2. Green cells made money and red cells lost it; each cell prints its net return.
3. The outlined cell is the candidate itself. A plateau of similar cells around it is more robust than a lone peak.
4. Grey cells have no return: an invalid pair (—), outside the legal range (·), never run (?), a failed run, or one that recorded none.

#### Good signs and warning signs

- Good: the candidate sits inside a broad green area.
- Warning: the candidate is a green island surrounded by red.
- Warning: a sharp diagonal edge. The two knobs only work together in a narrow combination.

#### An everyday comparison

A topographic map around a campsite. A campsite on a wide plateau is safe if you wander a little; one on a narrow ridge is not.

## Test over time

Test over time checks the search procedure itself. It cuts the development period into folds. Each fold searches again, from the same starting point and the same ranges, on its own training window only, then tests the winner it chose on the months right after. The current settings run on every test window too, as a benchmark. Every test result here is out-of-sample for the winner that produced it, and none of it judges a single candidate: it judges the way candidates are chosen.

### Fold timeline {#fold-timeline}

#### The question it answers

Which months did each fold train on and test on, and how did each fold end?

#### What you're looking at

A row for each fold. The grey bar is the fold's training window; the coloured bar right after it is its test window, in Eastern dates. A completed fold's test bar is green when its winner made money on the test window and red when it lost; a failed fold's bar is outlined in red; a grey one has not run yet. Before testing over time runs, the chart shows the folds the plan froze when the study was locked, and while it runs each fold fills in as it finishes.

Hover a fold to see its windows, the settings its training chose, the winner's training and test Sharpe, its retention, its test return and its test trades, or the reason it failed.

#### How to read it

1. Read each row as one fold: the grey bar is its training window, the coloured bar the test window right after it.
2. The test windows follow one another in time, so together they cover a stretch the procedure never trained on before choosing.
3. A green test bar made money on its test window and a red one lost; an outlined bar is a fold that failed, and a grey one has not run yet.
4. Hover a fold for its windows, both Sharpes, its retention, test return and trades.

#### Good signs and warning signs

- Good: every fold completed.
- Warning: failed folds. The verdict cannot judge a procedure with a failed fold, and the linked return breaks at it.
- Warning: mostly red test bars. The winners chosen on training lost money on the months that followed.
- Warning: very short test windows. A few weeks of testing says little.

#### An everyday comparison

A teacher who writes each week's quiz using only what was taught up to that week, then gives it the following week. Every quiz tests material the teacher could not tailor the questions to.

### Linked test return {#linked-return}

#### The question it answers

If the procedure had chosen fresh settings before each test window, how would its test results have added up?

#### What you're looking at

Each fold's test return, linked one after another as if each fold's result were reinvested in the next. The solid line is the search procedure; the dashed line is the current settings on the same test windows.

$$\text{linked return after fold } k = (1 + r_1)(1 + r_2) \cdots (1 + r_k) - 1$$

where each r is one fold's test return on its own fresh starting capital. A missing fold has no return, so the line breaks there and stays broken: linking needs every fold. Hover a fold for both lines' values.

This is the procedure's record, not any candidate's: each fold used a different winner.

#### How to read it

1. Follow the solid line: each fold’s test return, linked one after another.
2. Compare the dashed line: the current settings on the same test windows.
3. A gap in a line is a missing fold. Everything after it is cut off, because linking needs every fold.
4. Remember what it judges: the procedure that chose the settings, not any one candidate.

#### Good signs and warning signs

- Good: the solid line ends above the dashed one and above 0%.
- Warning: the solid line ends below the dashed one. Searching again did worse than keeping the current settings.
- Warning: a broken line. The record is incomplete.

#### An everyday comparison

A fund manager who rebalances every quarter using only past data, compared with simply holding what you already own. The question is whether the rebalancing habit paid, not whether any one quarter's picks were good.

### Training against test Sharpe {#train-test-sharpe}

#### The question it answers

How much of each fold's training Sharpe survived on its test window?

#### What you're looking at

Two bars per fold: the winner's Sharpe on its training window, in grey, and on its test window, in blue. The label above the test bar is its retention, the share of the training Sharpe it kept:

$$\text{retention} = \frac{\text{test Sharpe}}{\text{training Sharpe}}$$

Retention is defined only for a completed fold whose training Sharpe is above 0. The verdict takes the median retention over the folds that have one and needs 50% or more to say the procedure still worked.

#### How to read it

1. Each fold has two bars: the winner’s Sharpe on its training window, then on its test window.
2. Read the label above each test bar: the share of the training Sharpe it kept, its retention.
3. The verdict takes the median retention over folds and asks for 50% or more.
4. A fold with no label has no defined retention: it failed, or its training Sharpe was not above 0.

#### Good signs and warning signs

- Good: test bars a good fraction of the training bars, fold after fold.
- Warning: tall training bars with short or negative test bars. The search fitted each training window's noise.
- Warning: retention above 100% in one fold and far below in others. One lucky fold can hide the rest.

#### An everyday comparison

A runner's practice times against race times. Practice always looks better; the question is how much of it shows up on race day.

### Parameter drift {#parameter-drift}

#### The question it answers

Did the search choose similar settings in every fold?

#### What you're looking at

A row for each searched knob, on its own scale. The shaded band is the knob's searched range. The dots are the value each fold's training chose; a fold without a winner has no dot. A dot outside the band is a starting value the search kept because nothing it tried in the range did better. The dashed line is the all-period winner, the value the search chose on the whole development period; the dotted line is the current settings.

Hover a dot for the fold's value beside both reference values and the searched range.

#### How to read it

1. Each row is one searched knob on its own range; the dots are the value each fold’s training chose.
2. The dashed line is the all-period winner and the dotted line the current settings.
3. Dots that stay close together mean the search found the same answer each time.
4. Dots that jump around, or sit at the edge of the range, mean the best value depends on the period, or lies outside what was searched.

#### Good signs and warning signs

- Good: the fold winners cluster near the all-period winner.
- Warning: winners spread across the whole range. The knob's best value is unstable, so the all-period winner may be luck.
- Warning: winners pinned at the top or bottom of the range. A wider range might find a different answer.

#### An everyday comparison

Asking several tailors to measure you on different days. If they all write down nearly the same size, the size is real; if every one writes a different size, something is off with the measuring.

### Test return per fold {#fold-returns}

#### The question it answers

Fold by fold, did the procedure's winner beat the current settings on the same test window?

#### What you're looking at

Two bars per fold: the test return of the winner that fold's training chose, in blue, and the current settings' return on the same test window, in grey. Each return is on the fold's own fresh starting capital. Hover a fold for both returns and the difference between them:

$$\text{difference} = \text{winner's test return} - \text{current settings' test return}$$

A fold whose winner failed has only the grey bar.

#### How to read it

1. Each fold has two bars: the return of the winner its training chose, and the current settings on the same test window.
2. Compare them fold by fold. The grey bar is the benchmark.
3. Count the folds where the procedure leads. Winning a few folds by a lot is weaker than winning most of them.
4. A fold with only a grey bar is one whose winner failed; hover it for what the current settings did.

#### Good signs and warning signs

- Good: the blue bar ahead in most folds.
- Warning: the blue bar behind in most folds. Searching again tends to do worse than keeping what you have.
- Warning: one fold with a large lead and the rest behind.

#### An everyday comparison

Two route planners driving the same trips. One plans fresh each time; the other always takes the usual road. Count how often the fresh plan arrived first.

### Test activity per fold {#fold-activity}

#### The question it answers

Did the folds trade enough on their test windows to judge the procedure?

#### What you're looking at

On the left, each fold's test trades: the trades its winner made on its test window. On the right, all completed folds together, against the forward minimum, the dashed line:

$$\text{test trades} = \sum_{\text{completed folds}} \text{trades on the fold's test window}$$

The forward minimum is the number of trades all forward tests must reach together; for a plan with an expected trade frequency, it is that frequency over the forward tests' trading years, frozen when the study was locked. It applies to the total only: no fold has a minimum of its own. A total below it turns red. While testing over time is still running, the total counts the folds finished so far and is not judged against the minimum.

Hover a fold for its test trades and the current settings' trades on the same window.

#### How to read it

1. Each bar on the left is one fold’s test trades.
2. The bar on the right is all completed folds together.
3. The dashed line is the forward minimum: the trades all forward tests must reach together. It applies to the total, not to each fold.
4. A total below the line turns red: too few test trades to judge the procedure.

#### Good signs and warning signs

- Good: a total well above the line, spread across the folds.
- Warning: a total below the line. The verdict cannot call a result with so few trades.
- Warning: most of the trades in one fold. The others say little.

#### An everyday comparison

A survey that needs a thousand answers in total. A few hundred from each town is fine; nine hundred from one town is not much of a survey of the others.

## Compare

Compare lines up the candidates on the development data, the data used to choose them. The candidates are the all-period fit (blue), the recent fit (amber) and your current settings (grey, dashed).

### Candidate cards {#candidate-cards}

#### The question it answers

Which settings are you comparing, how did each do on the development data, and which one is selected?

#### What you're looking at

One card per candidate. A **candidate** is a complete set of knob values: the strategy's adjustable settings. Candidates that turned out to be exactly the same settings share one card, which says so.

Each card shows:

- Its name in its colour, where it came from (the all-period search, the most recent training window, or the frozen current settings), and a chip saying whether it passes the plan's rules: enough trades, a worst fall within the study's limit and a completed run.
- Its settings line: the value of every knob.
- Four numbers from the development data. **Net return** is the gain after trading costs, as a percent of the starting capital. **Sharpe** is return for each unit of day-to-day swing; higher means steadier. **Worst fall** is the deepest drop from a previous high on the engine's bar-by-bar equity. **Trades** is the count, beside the minimum the plan requires over the development period.
- A small line of the running return, the same series as the equity chart.

The button at the top of each card selects it. The charts below and the decision summary then explain the selected candidate.

#### How to read it

1. Find the selected card. The rest of the page explains that candidate; pick another card to switch.
2. Read its settings line: the value of every knob the search chose.
3. Compare net return, Sharpe and worst fall across the cards. More return with a much deeper fall is not a clear win.
4. Check trades against the minimum. A card below its minimum rests on too few trades to judge.
5. Glance at the small line: the same development return as the equity chart, at a glance.

#### Good signs and warning signs

- Good: a card that passes the rules with a Sharpe near or above 1 and a steadily climbing line.
- Warning: a card below its trade minimum. Too few trades means too little evidence, however good the rest looks.
- Warning: a worst fall flagged above the limit. The plan's rules rule that candidate out.

#### An everyday comparison

Think of player cards at a tryout: key numbers plus a form line. A player who missed most practices gets flagged even with good numbers, because the coach has seen too little.

### Equity and fall from peak {#equity-and-fall}

#### The question it answers

How did each candidate's account grow over the development period, and how deep and how long were its falls along the way?

#### What you're looking at

Two parts share one time axis, which runs across the development period one session at a time.

The top part shows each candidate's cumulative return: its running gain after trading costs, as a percent of the starting capital. The candidate you selected has the thicker line, and each line's last value is printed at its right edge.

The lower part, "Fall from peak", shows how far each account sits below its own highest point so far:

$$\text{fall from peak} = \frac{\text{equity today}}{\text{highest equity so far}} - 1$$

A line at 0% is at a new high. The selected candidate's fall is shaded, and a dashed line marks the study's worst-fall limit.

Each point is the account's value at a session's close. The rules judge the worst fall on the engine's bar-by-bar equity, which can dip lower during a day, so the worst fall on a candidate's card can be deeper than the lowest point here.

Hover any session to see every candidate's return and fall from peak at that close, beside the worst-fall limit.

#### How to read it

1. Read the labels at the right edge to see where each candidate finished.
2. Follow each line from left to right. A steady climb is more convincing than one big jump.
3. Drop to the lower part at the same date. A line below 0% there means that candidate is still recovering from a drop.
4. Find the lowest point of each line in the lower part. That is its deepest fall at a session close.
5. Note how long each line stays below 0%. Long stretches below a high are hard to sit through.

#### Good signs and warning signs

- Good: a top line that rises in many small steps, and a lower line that returns to 0% soon after each dip.
- Warning: a gain made in one short burst. Most of the result may rest on a few lucky trades.
- Warning: a fall still open at the right edge. The strategy was losing ground when the period ended.
- Warning: a lower line close to the worst-fall limit. The bar-by-bar fall the rules judge is at least this deep.

#### An everyday comparison

Picture a hike. The top part is your height above the trailhead; the lower part is how far you sit below the highest point you have reached so far. A hike can end high and still include a long, tiring descent.

### Candidates side by side {#side-by-side}

#### The question it answers

Which candidate leads on each measure, and does one lead on most of them?

#### What you're looking at

Six rows, one per measure, each with its own scale and its ends printed beneath. Each row holds one dot per candidate in its colour; the selected candidate's dots are larger. Further right is better on every row.

The six measures, all on the development data:

- **Net return**: the gain after trading costs, as a percent of the starting capital.
- **Sharpe**: return for each unit of day-to-day swing.
- **Worst fall**: the deepest drop from a previous high. This row is drawn reversed, so a smaller fall sits further right.
- **Trades a year**: development trades per trading year, counted on the exchange calendar. More trades means more evidence.
- **Win rate**: the share of trades that made money.
- **Stress runs in profit**: reruns under harsher costs and fills that still made money, out of the scenarios the plan scheduled.

Hover a dot to see every candidate's exact value on that measure. A value the study did not record has no dot.

#### How to read it

1. Find the selected candidate: its dots are the larger ones.
2. Read one row at a time. Each measure has its own scale, and further right is better on every row.
3. Compare the other dots on the same row. A dot well to the right of the selected one marks a measure where that candidate leads.
4. Weigh the rows together. A lead in return that comes with a deeper fall or fewer stress runs in profit is bought with risk.

#### Good signs and warning signs

- Good: one candidate near the right end on most rows, with a healthy trade count.
- Warning: a candidate that wins one row by a lot and trails on the rest.
- Warning: reading small gaps as big ones. Each row's scale spans only the candidates' own values, so check the printed ends first.

#### An everyday comparison

It is like comparing phones on battery, camera, price and weight. Each spec has its own units, so you compare one spec at a time, then decide which trade-offs you accept.

### Neighbor tornado {#neighbor-tornado}

#### The question it answers

Does the selected candidate's result hold when one setting moves one step?

#### What you're looking at

One row per searched knob. A **neighbor** is the candidate with one knob moved one search step down or up and every other knob unchanged.

The dashed line at 0 is the candidate's own net return, named with its value under the axis. Each row has two bars that start there: the lighter one for the neighbor one step below, the darker one for the neighbor one step above. A bar's length is how much that neighbor's net return differs from the candidate's: left of 0 it earns less, right of 0 it earns more. A neighbor that loses money is drawn in red, and its knob is marked "loses money".

A step can have no bar: it fell outside the knob's legal range, was not a valid combination, was not tested, or its run failed. Hover the knob's row to see which, and each neighbor's value, net return and change. "Show as table" lists every neighbor with its worst fall, trades and Sharpe.

#### How to read it

1. Start at the dashed line at 0: the candidate’s own net return. Each bar shows how far a one-step move changes it.
2. Read the lighter bars: each knob one step below the candidate’s value.
3. Read the darker bars: each knob one step above.
4. Look for a knob marked “loses money” and its red bar. A one-step change there turns the result into a loss.
5. Hover a knob to see each step’s value and net return, and why a step has no bar.

#### Good signs and warning signs

- Good: short bars on both sides of every knob, none of them red.
- Warning: a red bar. The result may depend on hitting one exact value.
- Warning: a missing step at the edge of a range. The best value sat at the end of what was searched, so a better or worse value may lie just past it.

#### An everyday comparison

A good cake recipe still works with a little more or less sugar. If one extra spoonful of salt ruins it, the recipe is fragile and may fail in someone else's kitchen.

### Cost stress ladder {#cost-stress}

#### The question it answers

Does the selected candidate still make money when trading costs are higher and fills are worse than the study assumes?

#### What you're looking at

Horizontal bars stacked like the rungs of a ladder, each the candidate's net return over the development period under one cost setting, with its value printed at its end.

The top bar, in the candidate's colour, is the result at the study's own costs: its commission per order, its slippage per share and its fill rule. Each bar below reruns the same settings in the engine under one scenario the plan declared before the study started, such as doubled slippage or fills at the next bar's open. A stress that turns the result into a loss is drawn in red and marked "loses money" under its name; a run that failed has no bar.

Hover a bar to see its net return, its change from the top bar, its worst fall, trades and Sharpe.

#### How to read it

1. Start with the top bar: the result at the study’s own costs.
2. Each bar below reruns the same settings with one harsher cost or fill.
3. Find the stressed bar with the lowest return. That stress is the one the result is most sensitive to.
4. A bar left of 0% marks a stress that turns the result into a loss.

#### Good signs and warning signs

- Good: every bar stays clearly right of 0%.
- Warning: a large drop from one stress, such as delayed fills. Trades that rely on an exact price are fragile.
- Warning: a stressed bar close to 0%. Real costs vary, and there would be little room left.

#### An everyday comparison

A bridge engineer tests a bridge with more weight than it should ever carry. If it still stands, the extra margin gives confidence. Here the extra weight is higher costs and slower fills.

## Compare: by month

The **By month** tab of Compare's evidence shows the selected candidate's development months, and how much of its result rests on its best month or its best few trades. The decision summary's Concentration row reports the same measure.

### Month calendar {#month-calendar}

#### The question it answers

Which months of the development period made money for the selected candidate, which lost it, and do the good and bad months fall in any pattern?

#### What you're looking at

A calendar with a row for each year and a column for each month. Each cell is one month of the development period, counted by its Eastern dates. Its colour is the month's net profit: green for a gain, red for a loss, the deeper the colour the bigger the amount, with $0 in the middle. Each cell also prints its amount with its sign, so the colour is never the only cue. A month outside the development period has no cell; it is never drawn as $0.

A month's net profit is the change in the account over the month, from the last session close of the month before (or the starting capital, for the first month) to the month's last session close:

$$\text{net profit}_m = \text{equity at the end of } m - \text{equity at the end of the month before}$$

A trade counts in the month it exits. Hover a cell to see the month's net profit, its return on the equity it started with, and how many trades closed in it.

#### How to read it

1. Read each row as a year and each column as a month. A month outside the development period has no cell.
2. Find the strongest colours: green months made the most, red months lost the most. Each cell prints its amount with its sign.
3. Look along each row for runs of red. Losing months in a row are harder to sit through than the same losses spread out.
4. Compare the same month across years. A month that is good every year may be a pattern; one good year is not.

#### Good signs and warning signs

- Good: green spread across most of the calendar, with the red months small and scattered.
- Warning: one or two deep green cells and pale colours elsewhere. The result rests on a few months; "Without its best" shows how much.
- Warning: a long run of red. A stretch like that is when people abandon a strategy.

#### An everyday comparison

A shop's monthly takings on a wall calendar. A good year can come from steady months, or from one holiday rush that covered for everything else.

### Monthly net profit {#monthly-net}

#### The question it answers

How did the selected candidate's development net profit move from one month to the next?

#### What you're looking at

One bar per month in time order: the same monthly net profit as the month calendar. A bar above $0 is a gain, in green; one below $0 is a loss, in red. The axis is in dollars.

Hover a bar to see the month's net profit, its return and how many trades closed in it. The table lists every month's values. The chart describes how performance changed; it claims no model of why.

#### How to read it

1. Read the bars left to right: each is one month’s net profit, a gain above $0 and a loss below.
2. Look for one bar much taller than the rest. A result that rests on one month is fragile; “Without its best” shows how much.
3. Check whether the losing months cluster together or are spread out.
4. Hover a month for its return and how many trades closed in it.

#### Good signs and warning signs

- Good: most bars above $0, of similar heights.
- Warning: the bars shrink over time. Recent months earn less than early ones.
- Warning: one bar taller than all the others put together.

#### An everyday comparison

A runner's monthly mileage. Steady months build fitness; one huge month followed by little says more about that month than about the runner.

### Profit concentration curve {#concentration-curve}

#### The question it answers

How fast does the selected candidate's development net profit pile up when its trades are counted from best to worst? A steep start means the result rests on a few big winners.

#### What you're looking at

A trade is one entry and the exit that closes it. Its net profit is what it made or lost after its entry and exit commission. The candidate's development trades are sorted from the biggest winner to the biggest loser.

The horizontal axis is the share of trades counted so far, from 0% to 100%. The vertical axis is their running net profit as a share of the final net profit. The shaded line is that running total. A dashed line at 100%, labelled "all net profit", marks the final result. A dot marks the best 5% of trades, rounded up, with the share of net profit they make together.

$$\text{share of net profit after } i \text{ trades} = \frac{\text{net profit of the best } i \text{ trades}}{\text{net profit of all the trades}}$$

The line climbs above 100% because the winners come first: the winners alone add up to more than the final result. Then the losers begin, smallest first, each pulling the total down. After the last and biggest loss, the line ends at 100%, because all the trades together make the final net profit.

The curve is drawn only when the trades, each net of its commission, add up to the run's net profit within a cent, and only when the run made money: a share of a loss means nothing. Otherwise the panel says why.

Hover the line to see how many trades are counted, their running net profit and its share of the whole.

#### How to read it

1. A steep climb at the left means the best few trades carry much of the profit.
2. The marked dot shows the share of net profit the best 5% of trades make.
3. The peak is where the winning trades end. Its height is all the winners added together.
4. The fall from the peak back to 100% is all the losing trades added together.

#### Good signs and warning signs

- Good: a steady climb over many trades, with the dot well below 100%.
- Warning: the curve passes 100% within the first few percent of trades. A handful of trades earned everything.
- Warning: the dot at or above 100%. Without those trades the result would be $0 or less, which the concentration rule flags.
- Warning: a very tall peak. The losses gave back most of what the winners made.

#### An everyday comparison

Sort a basketball team's games from biggest win to biggest loss and keep a running total of the point difference. The wins push the total above the season's final figure, and the losses pull it back down.

### Without its best {#without-best}

#### The question it answers

Does the selected candidate stay profitable over the development period without its best month, or without its best 5% of trades? This is the concentration rule, and the decision summary's Concentration row reports it.

#### What you're looking at

Three horizontal bars start at $0:

- **All trades**, in the candidate's colour, is the whole development net profit.
- **Without its best month** takes out the month with the highest net profit.
- **Without its best trades** takes out the best 5% of trades, rounded up, so at least one. Each trade counts its net profit after its entry and exit commission.

Each bar's value is printed to the cent beside it. A bar at $0 or less turns red and is marked "not profitable"; a loss reaches left of $0.

$$\text{without the best month} = \text{net profit} - \text{the best month's net profit}$$

$$\text{without the best trades} = \text{net profit} - \text{net profit of the best } \lceil n / 20 \rceil \text{ of the } n \text{ trades}$$

The rule reads **concern** when either result is $0 or less, to the cent, and **meets** otherwise. It reads **missing** when the run was not evaluated because the study's budget ran out, when it failed or made no trades, or when its trades do not add up to the run's net profit within a cent, for example because a position was still open when the period ended. Nothing is rescaled to make them fit. The measure informs your choice; it gates nothing. A study whose evidence was recorded before concentration was measured says "Not measured for this study".

Hover a bar to see what it takes out: the month and what it made, or the trades and what they made together.

#### How to read it

1. Start from the top bar: the development net profit with every trade.
2. Compare the middle bar with it. The gap is what the best month made.
3. Compare the bottom bar with it. The gap is what the best 5% of trades made together.
4. A bar at $0 or less turns red. That is the concern the decision summary reports.

#### Good signs and warning signs

- Good: both lower bars well above $0, each keeping a large share of the top bar's length. The profit came from many months and many trades.
- Warning: a bar at or below $0. The result rests on one month or a few trades that may never repeat.
- Warning: a bar just above $0. It passes with little room to spare.
- Warning: a large drop between bars, even when every bar stays above $0.

The rule sets a floor. Passing it does not show that the profit was evenly spread, and it promises nothing about future results.

#### An everyday comparison

A student's term grade looks strong. The teacher drops the best test, then separately the best few homework scores, and checks whether the grade still passes. If it fails either way, the grade depended on a couple of lucky days.

## Compare: trades

The **Trades** tab of Compare's evidence shows the selected candidate's development trades one by one. Each trade counts its net profit: its P&L before fees less its entry and exit commission. The engine already applies slippage to the fill prices.

$$\text{net profit of a trade} = \text{price change} \times \text{quantity} - 2 \times \text{commission per order}$$

The charts are drawn only when the trades, each net of its commission, add up to the run's net profit within a cent. When they do not, for example because a position was still open when the period ended, the tab says so and draws nothing. Nothing is rescaled to make them fit.

### Trade timeline {#trade-timeline}

#### The question it answers

When did the selected candidate make its development profit, trade by trade?

#### What you're looking at

Two parts share one time axis, dated in your local time. The upper line is the running net profit: after each trade exits, the net profit of that trade and every trade before it. It steps at each exit and stays level until the next. A dashed line marks $0. Beneath it, each trade is a thin bar at its exit time, as tall as its own net profit: green for a gain, red for a loss.

$$\text{running net profit after trade } k = \sum_{i \le k} \text{net profit of trade } i$$

The trades are in exit order. Hover a trade to see its full record: when it entered and exited (in your local time), its entry and exit prices, quantity, P&L before fees, net profit, the running total, the decision bars it was held, the RSI at entry and why it exited. The table lists every trade.

#### How to read it

1. Follow the upper line: the running net profit after each trade, in exit order.
2. Find where it rose most. Profit made in a few short stretches is less dependable than a steady climb.
3. Find the line’s lowest point. Below $0 there means the trades had lost money overall by then.
4. Read the bars beneath: each trade’s own net profit, at its exit.
5. Hover a trade for its full record: times, prices, quantity, P&L before and after commission, bars held, RSI at entry and why it exited.

#### Good signs and warning signs

- Good: a line that climbs across the whole period, with no long flat or falling stretches.
- Warning: a line that is flat for most of the period and jumps in one place.
- Warning: a line that ends near where it was long ago. The later trades gave back what the earlier ones made.

#### An everyday comparison

A savings account statement: each deposit or withdrawal, with the balance after it. A balance that grows with every pay cheque is steadier than one carried by a single windfall.

### Trade net profit histogram {#trade-histogram}

#### The question it answers

What do the selected candidate's wins and losses look like: many small ones, or a few big ones?

#### What you're looking at

Each bar counts the development trades whose net profit falls in its range. The ranges are all the same width, set by the Freedman–Diaconis rule from how spread out the middle half of the trades is:

$$\text{bin width} = 2 \times \text{IQR} \times n^{-1/3}$$

where IQR is the gap between the 25th and 75th percentile of the trades' net profits and n is the number of trades. The bins are lined up so that $0 is always an edge: no bin holds both a win and a loss. Bins left of $0 hold losing trades, in red; the rest, in green. Empty bins between are kept, at zero trades.

The trades are binned to the cent. When the middle half of the trades net the same, the width falls back to the trades' full range divided by Sturges' bin count; an extreme outlier can widen the bins so that there are never more than 60; and when every trade nets the same, there is one bin. The hover names the rule that set the width.

Hover a bar to see its range and how many trades, wins and losses it holds.

#### How to read it

1. Each bar counts the trades whose net profit falls in its range. Bars left of $0 are losing trades.
2. Read the winning side: are the wins many and small, or few and large?
3. Compare the tails. A long tail on the losing side means a few trades lost far more than usual.
4. Hover a bar for its range and how many trades fell in it.

#### Good signs and warning signs

- Good: losses bunched close to $0, with the winning side reaching further out.
- Warning: a long tail of losses. Occasional large losses can undo many small wins.
- Warning: most trades in the bins just left of $0. Commission alone may be turning small moves into losses.

#### An everyday comparison

A teacher's spread of test scores. The average can hide whether most students did fine and a few failed badly, or everyone scraped by.

### Hold time against net profit {#hold-time}

#### The question it answers

Does holding a trade for longer help or hurt the selected candidate's results?

#### What you're looking at

Each dot is a development trade. Across is how many decision bars it was held; up is its net profit. A decision bar is the strategy's own bar: the program decides on 15-minute bars, so a trade held 5 bars was held for 75 minutes of trading.

$$\text{bars held} = \text{the number of decision bars that close after the entry and by the exit}$$

The bars are counted on the trading calendar: nights, weekends and holidays hold none, and a half-day holds fewer. They are counted on the calendar between the entry and exit fills, so a trade can show more bars than the strategy's hold when the strategy did not see every bar the calendar schedules. When the strategy's bars do not line up with the session's half hours, or its cadence cannot be read, the panel says so and counts nothing. Circles are trades the strategy closed itself. Diamonds are trades closed because the tested window ended. A dashed line marks $0.

A strategy that exits after a fixed number of bars puts most of its trades in one column. That column's spread of results is what the hold time delivers.

Hover a dot to see the trade's record.

#### How to read it

1. Each dot is a trade: the decision bars it was held across, and its net profit.
2. Read the circles: trades the strategy closed itself. A strategy that exits on a timer holds most of its trades for the same number of bars.
3. Find any diamonds: trades closed because the tested window ended, not by the strategy.
4. Compare the spread above and below $0 at each hold. If longer holds lose more often, holding longer is not helping.

#### Good signs and warning signs

- Good: at the strategy's own hold, more dots above $0 than below, and the wins reaching further than the losses.
- Warning: a few diamonds with large results. The window's end, not the strategy, closed those trades, and they may not repeat.
- Warning: where holds vary, the longer ones mostly below $0.

#### An everyday comparison

Leaving bread in the oven for a fixed time. If the loaves on the timer come out well most days, the timer works; if they burn as often as not, the time is wrong.

### RSI at entry against net profit {#entry-rsi}

#### The question it answers

Within the RSI gates the strategy enters between, do some RSI readings lead to better trades than others?

#### What you're looking at

Each dot is a development trade: across is the RSI the strategy saw when it decided to enter, and up is the trade's net profit. The two dashed lines are this candidate's lower and upper RSI gates; every entry falls between them.

The level lines are band averages. The space between the gates is cut every 5 RSI points, and each band's line is the average net profit of the trades that entered in it:

$$\text{band average} = \frac{\text{net profit of the trades entered in the band}}{\text{number of those trades}}$$

A band runs up to, but not including, its upper edge; the last band includes the upper gate. A band no trade entered in has no line. A trade with no RSI recorded is in no band, and the summary says how many.

Hover a dot for the trade, or a band's line for its trade count and average.

#### How to read it

1. Each dot is a trade: the RSI the strategy saw when it decided to enter, and the trade’s net profit.
2. The dashed lines are the RSI gates. Every entry falls between them.
3. Read the level lines: the average net profit of the trades in each 5-point band.
4. Look for a band whose average is clearly better or worse than the rest, and check its trade count before trusting it.

#### Good signs and warning signs

- Good: similar band averages across the gates. The result does not depend on a narrow slice of RSI.
- Warning: one band carries the profit and the others lose. Tighter gates might look better, but that is a new search on the same data, which is how a result gets overfitted.
- Warning: a striking band average built on two or three trades.

#### An everyday comparison

A fishing log by water temperature. If every catch came at one narrow temperature, that may be real, or it may be a handful of lucky days.

### Entry time and weekday {#entry-time}

#### The question it answers

Do the selected candidate's trades do better or worse at some times of day or on some weekdays?

#### What you're looking at

A grid with a row for each weekday and a column for each Eastern half hour of the trading session. Each trade sits in the cell of the weekday and half hour it entered in, in Eastern time whatever your own time zone. The number in a cell is how many trades entered then. A cell with no number had no entries.

A cell's colour is the average net profit of its trades: green for a gain, red for a loss, deeper for more. A cell with fewer than five trades is grey with a dashed outline instead: its average rests on too few trades to mean much.

$$\text{cell average} = \frac{\text{total net profit of the cell's trades}}{\text{number of those trades}}$$

Hover a cell for its trade count, average and total net profit.

#### How to read it

1. Each cell is a weekday and an Eastern half hour; its number is how many trades entered then.
2. Read the coloured cells: green where those trades made money on average, red where they lost.
3. Grey cells hold too few trades to judge. Don’t read a pattern into them.
4. Look for a row or column that is red or green across several cells. A real pattern shows in more than one cell.

#### Good signs and warning signs

- Good: no strong pattern, or one that holds across several neighbouring cells with plenty of trades.
- Warning: most cells grey. The development period has too few trades to say anything about timing.
- Warning: one bright cell among grey and pale ones. Treat it as luck until more data says otherwise.

#### An everyday comparison

A café's takings by hour and weekday. A busy Saturday morning across many weeks is a pattern worth staffing for; one big Tuesday at 3 pm is not.
