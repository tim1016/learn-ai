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

### Equity and fall from peak {#equity-and-fall}

#### The question it answers

How did each candidate's account grow over the development period, and how deep and how long were its falls along the way?

#### What you're looking at

Two parts share one time axis, which runs across the development period one session at a time.

The top part shows each candidate's cumulative return: its running gain after trading costs, as a percent of the starting capital. The candidate you selected has the thicker line, and each line's last value is printed at its right edge.

The lower part, "Fall from peak", shows how far each account sits below its own highest point so far:

$$\text{fall from peak} = \frac{\text{equity today}}{\text{highest equity so far}} - 1$$

A line at 0% is at a new high. The selected candidate's fall is shaded, and a dashed line marks the study's worst-fall limit.

Each point is the account's value at a session's close. The rules judge the worst fall on the engine's bar-by-bar equity, which can dip lower during a day, so a candidate's worst fall in the candidate table can be deeper than the lowest point here.

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
