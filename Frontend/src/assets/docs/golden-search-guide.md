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
