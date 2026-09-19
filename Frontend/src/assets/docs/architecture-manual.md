# How learn-ai is built

*A plain-language architecture manual for the owner. Checked against the code on 2026-09-18 (commit `a69b77d5`).*

This manual explains how learn-ai is put together: what its parts are, how they hand work to each other, where the one true answer to each kind of question lives, and where the design is weakest. It is written for someone who needs to understand the architecture well enough to make decisions and brief AI agents, without reading code.

## The whole system in six sentences

1. learn-ai is a set of cooperating programs that all run on **one Mac**: a web app you look at, two back-office services that compute and store, and one **clerk** per brokerage account.
2. The **clerks** are the only parts allowed to touch money: each owns exactly one Alpaca account, its own disk, its own bots and its own tamper-evident ledger.
3. The **coordinator** is a switchboard: it routes every account command to the right clerk and keeps receipts, but it never holds money and never trades.
4. **Live prices come only from IBKR** (read-only) and **orders go only to Alpaca**. That split is a standing owner decision.
5. A strategy earns the right to trade through a fixed road: data → research → proof → deployment on one account, with a human decision at each gate that matters.
6. Every important question has **one place that answers it**, and wherever a machine can check that no second copy has crept in, the build checks it on every change.

## How to read this manual

- **It describes structure, which changes slowly**: who owns what, which way things flow, where each truth lives. It deliberately leaves out things that change weekly, such as counts, which containers happen to be running, and open bugs. Those live in the trackers linked from [chapter 7](#7-weak-spots-built-into-the-design).
- **It is a map, not an authority.** Every decision it mentions was made in an Architecture Decision Record (ADR) or a canonical document, and it links to them. If this manual and one of those disagree, the linked document wins, and the code wins over both.
- **Each chapter says when it was checked.** If the code has moved a lot since, re-check a detail before relying on it.
- **It uses the real names** your code and your AI agents use (*clerk*, *lane*, *coordinator*), each explained the first time and collected in the [glossary](#8-glossary).
- **Diagrams share one colour key.** On the big map in chapter 1, click a box to jump to its chapter.

<figure class="md-diagram">
<svg viewBox="0 0 960 110" role="img" aria-label="Colour key: blue is the coordinator, orange is the Live lane, light blue is the Paper lane, purple is stored data, dashed outline is an outside company, grey is other parts. Blue-green arrows carry prices and data, blue arrows carry commands, orange arrows carry orders and money, red dashed arrows mean refused or not built.">
<defs><marker id="ah0" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path class="dg-arrowhead" d="M0,0 L10,5 L0,10 z"/></marker></defs>
<rect class="dg-box dg-coord" x="10" y="10" width="148" height="44" rx="6"/><text class="dg-h" x="84" y="29" text-anchor="middle">Coordinator</text><text class="dg-s" x="84" y="46" text-anchor="middle">the switchboard</text>
<rect class="dg-box dg-live" x="168" y="10" width="148" height="44" rx="6"/><text class="dg-h" x="242" y="29" text-anchor="middle">Live lane</text><text class="dg-s" x="242" y="46" text-anchor="middle">real money</text>
<rect class="dg-box dg-paper" x="326" y="10" width="148" height="44" rx="6"/><text class="dg-h" x="400" y="29" text-anchor="middle">Paper lane</text><text class="dg-s" x="400" y="46" text-anchor="middle">practice money</text>
<rect class="dg-box dg-store" x="484" y="10" width="148" height="44" rx="6"/><text class="dg-h" x="558" y="29" text-anchor="middle">Stored data</text><text class="dg-s" x="558" y="46" text-anchor="middle">files and databases</text>
<rect class="dg-box dg-outside" x="642" y="10" width="148" height="44" rx="6"/><text class="dg-h" x="716" y="29" text-anchor="middle">Outside company</text><text class="dg-s" x="716" y="46" text-anchor="middle">not ours</text>
<rect class="dg-box" x="800" y="10" width="148" height="44" rx="6"/><text class="dg-h" x="874" y="29" text-anchor="middle">Other parts</text><text class="dg-s" x="874" y="46" text-anchor="middle">screens and services</text>
<path class="dg-flow dg-data" d="M10,86 L70,86" marker-end="url(#ah0)"/><text class="dg-t" x="80" y="90">prices and data</text>
<path class="dg-flow dg-command" d="M250,86 L310,86" marker-end="url(#ah0)"/><text class="dg-t" x="320" y="90">commands</text>
<path class="dg-flow dg-money" d="M470,86 L530,86" marker-end="url(#ah0)"/><text class="dg-t" x="540" y="90">orders and money</text>
<path class="dg-flow dg-blocked" d="M720,86 L780,86" marker-end="url(#ah0)"/><text class="dg-t" x="790" y="90">refused or not built</text>
</svg>
<figcaption>The colour key used by every diagram in this manual.</figcaption>
</figure>

---

## 1. The one-picture map

*Checked against the code on 2026-09-18 (commit `a69b77d5`).*

> **In one sentence:** every part of learn-ai runs on your Mac. The screens only show, the Python coordinator computes and routes, and one clerk per brokerage account is the only thing that touches money.

<figure class="md-diagram">
<svg viewBox="0 0 960 660" role="img" aria-label="Map of learn-ai. Outside companies at the top: Polygon, FRED and IBKR. Inside your Mac: the web app talks to the .NET backend and the Python coordinator; the .NET backend relays to the coordinator; the coordinator reads Polygon and FRED, writes the data lake, uses Postgres and Redis, and routes commands to a Live clerk and a Paper clerk. IB Gateway brings IBKR's live prices to both clerks. Both clerks send orders to Alpaca, the outside company at the bottom.">
<defs><marker id="ah1" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path class="dg-arrowhead" d="M0,0 L10,5 L0,10 z"/></marker></defs>
<text class="dg-s" x="20" y="40">OUTSIDE COMPANIES</text>
<rect class="dg-box dg-outside" x="290" y="12" width="150" height="48" rx="6"/><text class="dg-h" x="365" y="32" text-anchor="middle">Polygon</text><text class="dg-t" x="365" y="50" text-anchor="middle">price history</text>
<rect class="dg-box dg-outside" x="460" y="12" width="120" height="48" rx="6"/><text class="dg-h" x="520" y="32" text-anchor="middle">FRED</text><text class="dg-t" x="520" y="50" text-anchor="middle">economic data</text>
<rect class="dg-box dg-outside" x="690" y="12" width="150" height="48" rx="6"/><text class="dg-h" x="765" y="32" text-anchor="middle">IBKR</text><text class="dg-t" x="765" y="50" text-anchor="middle">live prices only</text>
<rect class="dg-zone" x="8" y="76" width="944" height="486" rx="10"/>
<text class="dg-s" x="22" y="98">YOUR MAC · EVERYTHING INSIDE THIS LINE RUNS HERE</text>
<path class="dg-flow dg-data" d="M365,60 L365,188" marker-end="url(#ah1)"/>
<path class="dg-flow dg-data" d="M520,60 L520,188" marker-end="url(#ah1)"/>
<path class="dg-flow dg-data" d="M765,60 L765,104" marker-end="url(#ah1)"/>
<path class="dg-flow dg-command" d="M130,182 L130,288" marker-end="url(#ah1)"/><text class="dg-label" x="138" y="240">GraphQL · jobs</text>
<path class="dg-flow dg-command" d="M230,151 L255,151 L255,215 L278,215" marker-end="url(#ah1)"/><text class="dg-label" x="262" y="182">requests</text>
<path class="dg-flow dg-command" d="M230,321 L255,321 L255,262 L278,262" marker-end="url(#ah1)"/><text class="dg-label" x="262" y="304">relays</text>
<path class="dg-flow dg-command" d="M540,236 L604,236" marker-end="url(#ah1)"/><text class="dg-label" x="572" y="226" text-anchor="middle">commands</text>
<path class="dg-flow dg-data" d="M720,152 L720,224" marker-end="url(#ah1)"/>
<path class="dg-flow dg-data" d="M810,152 L810,224" marker-end="url(#ah1)"/>
<text class="dg-label" x="765" y="186" text-anchor="middle">read-only prices</text>
<path class="dg-flow" d="M115,352 L115,428" marker-end="url(#ah1)"/>
<path class="dg-flow" d="M300,282 L300,400 L150,400 L150,428" marker-end="url(#ah1)"/>
<path class="dg-flow" d="M340,282 L340,428" marker-end="url(#ah1)"/>
<path class="dg-flow dg-data" d="M475,282 L475,428" marker-end="url(#ah1)"/><text class="dg-label" x="482" y="370">only writer</text>
<path class="dg-flow dg-money" d="M695,310 L695,586" marker-end="url(#ah1)"/>
<path class="dg-flow dg-money" d="M861,310 L861,586" marker-end="url(#ah1)"/>
<text class="dg-label" x="778" y="470" text-anchor="middle">orders out · fills back</text>
<a href="#6-who-decides-what"><rect class="dg-box" x="30" y="120" width="200" height="62" rx="6"/><text class="dg-h" x="130" y="146" text-anchor="middle">Web app</text><text class="dg-t" x="130" y="166" text-anchor="middle">screens · decides nothing</text></a>
<a href="#4-the-seams"><rect class="dg-box" x="30" y="290" width="200" height="62" rx="6"/><text class="dg-h" x="130" y="316" text-anchor="middle">.NET backend</text><text class="dg-t" x="130" y="336" text-anchor="middle">older service · jobs</text></a>
<a href="#3-account-lanes"><rect class="dg-box dg-coord" x="280" y="190" width="260" height="92" rx="6"/><text class="dg-h" x="410" y="216" text-anchor="middle">Python coordinator</text><text class="dg-t" x="410" y="238" text-anchor="middle">research · history · data lake</text><text class="dg-t" x="410" y="258" text-anchor="middle">switchboard for account commands</text></a>
<a href="#36-where-live-prices-come-from"><rect class="dg-box" x="690" y="106" width="150" height="46" rx="6"/><text class="dg-h" x="765" y="126" text-anchor="middle">IB Gateway</text><text class="dg-t" x="765" y="144" text-anchor="middle">IBKR's program</text></a>
<rect class="dg-zone" x="606" y="196" width="340" height="128" rx="8"/>
<a href="#3-account-lanes"><rect class="dg-box dg-live" x="620" y="226" width="150" height="84" rx="6"/><text class="dg-h" x="695" y="252" text-anchor="middle">Live clerk</text><text class="dg-t" x="695" y="272" text-anchor="middle">real-money account</text><text class="dg-t" x="695" y="292" text-anchor="middle">own disk · own bots</text></a>
<a href="#3-account-lanes"><rect class="dg-box dg-paper" x="786" y="226" width="150" height="84" rx="6"/><text class="dg-h" x="861" y="252" text-anchor="middle">Paper clerk</text><text class="dg-t" x="861" y="272" text-anchor="middle">practice account</text><text class="dg-t" x="861" y="292" text-anchor="middle">own disk · own bots</text></a>
<text class="dg-label" x="614" y="342">LANES · ONE CLERK PER ACCOUNT</text>
<a href="#5-single-sources-of-truth"><rect class="dg-box dg-store" x="30" y="430" width="170" height="56" rx="6"/><text class="dg-h" x="115" y="454" text-anchor="middle">Postgres</text><text class="dg-t" x="115" y="474" text-anchor="middle">shared records</text></a>
<a href="#5-single-sources-of-truth"><rect class="dg-box dg-store" x="222" y="430" width="140" height="56" rx="6"/><text class="dg-h" x="292" y="454" text-anchor="middle">Redis</text><text class="dg-t" x="292" y="474" text-anchor="middle">scratch pad</text></a>
<a href="#5-single-sources-of-truth"><rect class="dg-box dg-store" x="390" y="430" width="170" height="56" rx="6"/><text class="dg-h" x="475" y="454" text-anchor="middle">Data lake</text><text class="dg-t" x="475" y="474" text-anchor="middle">price-history files</text></a>
<text class="dg-s" x="20" y="618">OUTSIDE COMPANY</text>
<rect class="dg-box dg-outside" x="620" y="588" width="316" height="52" rx="6"/><text class="dg-h" x="778" y="610" text-anchor="middle">Alpaca</text><text class="dg-t" x="778" y="628" text-anchor="middle">accounts · orders · fills</text>
</svg>
<figcaption>Click a box to jump to its chapter. Money moves only through the clerks; market history comes only through the coordinator; the screens only show.</figcaption>
</figure>

### The parts, and what each one keeps

| Part | Its job, in plain words | What it keeps |
|---|---|---|
| **Web app** (Angular) | The screens. It shows what the back office tells it and sends your commands. It decides nothing about safety. | Nothing. |
| **.NET backend** | The older back-office service. It answers some screens through GraphQL, hands long jobs to Python, and keeps a few records of its own: Data Lab saved sessions and a practice portfolio tracker. It never talks to a broker. The owner has said it is being phased out: no new feature adds a .NET table. | Its own tables in Postgres. |
| **Python coordinator** (the "data plane") | The workhorse and the switchboard. It runs research and backtests, fetches price history, is the only writer of the data lake, and routes every account command to the right clerk. It never connects to Alpaca or IBKR. | The data lake, research results, and the fleet registry (which lanes exist). |
| **Live clerk** and **Paper clerk** | One per brokerage account. Each owns that account's ledger, its bots, its Alpaca connection and its own read-only IBKR price feed. | Everything about its one account, on its own disk. |
| **Postgres** | The shared database. Some tables belong to .NET, some to Python, and it holds the data lake's catalog (never the prices themselves). | Records. |
| **Redis** | A scratch pad for job progress, shared by .NET and Python. It forgets everything on restart, on purpose. | Nothing durable. |
| **Data lake** | A folder of price-history files, each fingerprinted, that every backtest and history chart reads. | Price history. |
| **IB Gateway** | IBKR's own program, running directly on the Mac rather than in a container. It is the live price source. | Nothing of ours. |
| **LEAN launcher** | A small helper on the Mac that starts a LEAN backtest container when Python asks for a second-opinion run. | Nothing. |

**The outside companies** are Polygon (price history and reference data), IBKR (live prices only, read-only; no order is ever placed there), Alpaca (accounts, orders, fills) and FRED (economic data).

### Three things the map shows that matter most

- **Money has exactly one road.** Orders reach Alpaca only from a clerk. No other part holds broker credentials or a broker connection.
- **History has exactly one road.** Only the coordinator calls Polygon and writes the data lake. Even a clerk that needs a chart asks the coordinator; clerks are given no Polygon key.
- **The screens hold no judgment.** Whether a button is allowed, whether a bot is safe, what a warning says: all of it is written by Python and only displayed by the web app. [Chapter 6](#6-who-decides-what) explains why.

### Two ways to run the same program

The Python coordinator and the clerks are **the same program**. One setting, its **role**, decides which parts switch on:

- **Fleet posture** (what `restart.sh` runs today): one coordinator plus one clerk per account, each a separate process with its own disk. This is the posture this manual describes.
- **Combined posture**: the older single process that does every job at once. It stays as a development and rollback option, and it runs *without* the lane fences described in chapter 3. That is one of the weak spots in [chapter 7](#7-weak-spots-built-into-the-design).

---

## 2. The journey of a strategy

*Checked against the code on 2026-09-18 (commit `a69b77d5`).*

> **In one sentence:** a strategy moves from data to research to proof to deployment on one account, and at every gate that matters, either a machine check or a human decision must pass before it can move on.

<figure class="md-diagram">
<svg viewBox="0 0 980 420" role="img" aria-label="The journey of a strategy, left to right: data, research, prove, deploy, trade, evidence. Under each stage is the gate it must pass. A band under deploy and trade lists what Live adds: graduation, arming, cash bound and daily loss hold, and required corpus coverage.">
<defs><marker id="ah2" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path class="dg-arrowhead" d="M0,0 L10,5 L0,10 z"/></marker></defs>
<rect class="dg-box" x="10" y="20" width="148" height="130" rx="6"/><text class="dg-h" x="84" y="44" text-anchor="middle">1 · Data</text><text class="dg-s" x="84" y="62" text-anchor="middle">coordinator</text><text class="dg-t" x="84" y="88" text-anchor="middle">Polygon → data lake</text><text class="dg-t" x="84" y="108" text-anchor="middle">fingerprinted files</text><text class="dg-t" x="84" y="128" text-anchor="middle">one writer: Python</text>
<rect class="dg-box" x="172" y="20" width="148" height="130" rx="6"/><text class="dg-h" x="246" y="44" text-anchor="middle">2 · Research</text><text class="dg-s" x="246" y="62" text-anchor="middle">coordinator</text><text class="dg-t" x="246" y="88" text-anchor="middle">Strategy Lab runs</text><text class="dg-t" x="246" y="108" text-anchor="middle">Grid Search</text><text class="dg-t" x="246" y="128" text-anchor="middle">Walk-Forward</text>
<rect class="dg-box" x="334" y="20" width="148" height="130" rx="6"/><text class="dg-h" x="408" y="44" text-anchor="middle">3 · Prove</text><text class="dg-s" x="408" y="62" text-anchor="middle">coordinator + you</text><text class="dg-t" x="408" y="88" text-anchor="middle">Golden Validation</text><text class="dg-t" x="408" y="108" text-anchor="middle">build receipt</text><text class="dg-t" x="408" y="128" text-anchor="middle">corpus coverage</text>
<rect class="dg-box" x="496" y="20" width="148" height="130" rx="6"/><text class="dg-h" x="570" y="44" text-anchor="middle">4 · Deploy</text><text class="dg-s" x="570" y="62" text-anchor="middle">you, at one account</text><text class="dg-t" x="570" y="88" text-anchor="middle">Deploy strategy tab</text><text class="dg-t" x="570" y="108" text-anchor="middle">sealed, never edited</text><text class="dg-t" x="570" y="128" text-anchor="middle">one Start decision</text>
<rect class="dg-box" x="658" y="20" width="148" height="130" rx="6"/><text class="dg-h" x="732" y="44" text-anchor="middle">5 · Trade</text><text class="dg-s" x="732" y="62" text-anchor="middle">the account's clerk</text><text class="dg-t" x="732" y="88" text-anchor="middle">IBKR bars → strategy</text><text class="dg-t" x="732" y="108" text-anchor="middle">clerk vets each order</text><text class="dg-t" x="732" y="128" text-anchor="middle">orders to Alpaca</text>
<rect class="dg-box" x="820" y="20" width="148" height="130" rx="6"/><text class="dg-h" x="894" y="44" text-anchor="middle">6 · Evidence</text><text class="dg-s" x="894" y="62" text-anchor="middle">the account's clerk</text><text class="dg-t" x="894" y="88" text-anchor="middle">sealed ledger</text><text class="dg-t" x="894" y="108" text-anchor="middle">fills and every run</text><text class="dg-t" x="894" y="128" text-anchor="middle">replayable bar record</text>
<path class="dg-flow" d="M158,85 L170,85" marker-end="url(#ah2)"/><path class="dg-flow" d="M320,85 L332,85" marker-end="url(#ah2)"/><path class="dg-flow" d="M482,85 L494,85" marker-end="url(#ah2)"/><path class="dg-flow" d="M644,85 L656,85" marker-end="url(#ah2)"/><path class="dg-flow" d="M806,85 L818,85" marker-end="url(#ah2)"/>
<text class="dg-s" x="10" y="182">THE GATE AT EACH STAGE</text>
<rect class="dg-box dg-good" x="10" y="192" width="148" height="92" rx="6"/><text class="dg-t" x="84" y="216" text-anchor="middle">a backtest refuses</text><text class="dg-t" x="84" y="236" text-anchor="middle">to run if any day</text><text class="dg-t" x="84" y="256" text-anchor="middle">is missing</text>
<rect class="dg-box dg-good" x="172" y="192" width="148" height="92" rx="6"/><text class="dg-t" x="246" y="216" text-anchor="middle">an engineer turns</text><text class="dg-t" x="246" y="236" text-anchor="middle">the idea into a</text><text class="dg-t" x="246" y="256" text-anchor="middle">registered program</text>
<rect class="dg-box dg-good" x="334" y="192" width="148" height="92" rx="6"/><text class="dg-t" x="408" y="216" text-anchor="middle">your review record</text><text class="dg-t" x="408" y="236" text-anchor="middle">for the exact setup;</text><text class="dg-t" x="408" y="256" text-anchor="middle">tested code = running</text>
<rect class="dg-box dg-good" x="496" y="192" width="148" height="92" rx="6"/><text class="dg-t" x="570" y="216" text-anchor="middle">strategy + account</text><text class="dg-t" x="570" y="236" text-anchor="middle">pair approved; fresh,</text><text class="dg-t" x="570" y="256" text-anchor="middle">reconciled, flat</text>
<rect class="dg-box dg-good" x="658" y="192" width="148" height="92" rx="6"/><text class="dg-t" x="732" y="216" text-anchor="middle">every entry: account</text><text class="dg-t" x="732" y="236" text-anchor="middle">holds checked; intent</text><text class="dg-t" x="732" y="256" text-anchor="middle">saved before sending</text>
<rect class="dg-box dg-good" x="820" y="192" width="148" height="92" rx="6"/><text class="dg-t" x="894" y="216" text-anchor="middle">every position has</text><text class="dg-t" x="894" y="236" text-anchor="middle">an in-app path</text><text class="dg-t" x="894" y="256" text-anchor="middle">back to flat</text>
<rect class="dg-box dg-live" x="496" y="310" width="310" height="96" rx="6"/><text class="dg-h" x="651" y="334" text-anchor="middle">Live adds</text><text class="dg-t" x="651" y="356" text-anchor="middle">account graduation · per-bot arming that expires</text><text class="dg-t" x="651" y="376" text-anchor="middle">cash-bound entries · daily loss hold</text><text class="dg-t" x="651" y="396" text-anchor="middle">corpus coverage required</text>
</svg>
<figcaption>Six stages, each with its gate. Green boxes are checkpoints; the orange band is what real money adds on top.</figcaption>
</figure>

### Stage 0: an idea becomes code

A strategy can be deployed only after an engineer (in practice, you and an AI agent) has written it as code and **registered** it as a program. Research tools can explore ideas, but none of them can create a deployable strategy on its own. The deploy screen only offers programs from that registry.

### Stage 1: data

- **Where history comes from.** Historical prices come from Polygon, and only the Python coordinator calls Polygon. (A .NET component happens to be called "PolygonService", but it actually calls Python.)
- **Where history is kept.** The **data lake** is a folder of price-history files, one per symbol, trading day and bar size, kept in the same file format LEAN uses. Adjusted and raw prices sit in separate folders. Every file carries a fingerprint (a SHA-256 hash), so you can prove later exactly which bytes a backtest read.
- **The index is not the data.** A catalog in Postgres records which files exist, their fingerprints, and who is currently fetching each one. It never holds prices. If the catalog is lost, the recovery is to wipe it and fetch again; there is no rebuild tool.
- **One writer.** Only the coordinator's data-lake module may write lake files. Everyone else reads. If two jobs need the same day, it is downloaded once: the catalog gives one job a lease, and the file and its fingerprint are published together.
- **How data arrives.** Three ways: a backfill you start from the Data Lake Observatory, a backtest fetching days it is missing, or a chart request. **A backtest refuses to run if the lake cannot supply every day it needs**; it never quietly runs on partial data.
- **The calendar.** One calendar module decides trading days, open and close times and half-days (see [chapter 5](#5-single-sources-of-truth)).
- **Known exception.** A few research tools still call Polygon directly and skip the lake. That is a recorded decision in [ADR 0049](https://github.com/tim1016/learn-ai/blob/master/docs/architecture/adrs/0049-data-lake-is-the-market-data-authority.md), not an accident.

### Stage 2: research

- **One official engine, one second opinion.** The Python backtest engine gives the official answer. **LEAN** (QuantConnect's engine, run in a sealed-off container) is a second opinion used to check it; it never overrides it. Who owns which engine is recorded in the [engine authority map](https://github.com/tim1016/learn-ai/blob/master/docs/architecture/engine-authority-map.md).
- **Strategy Lab** runs a backtest as a long job: .NET gives it an ID, tracks its progress in Redis and hands it to Python. Python saves every run to its research tables. For strategies with a LEAN twin, it launches the matching LEAN run automatically and records a **parity verdict** comparing the two.
- **Grid Search** tries many parameter settings over one time window and scores each one. Its results are "in-sample" by construction: they tell you what fit the past best, not what will work.
- **Walk-Forward** is the honest version: it splits history into folds, picks the best setting on each training slice, tests it on the next unseen slice, and freezes a verdict computed by fixed code.
- **Research Lab** holds exploratory studies. Its "graduation ladder" grades an *idea*; it has nothing to do with graduating a *Live account*, and none of it feeds a deploy gate.

### Stage 3: prove

This is where the platform's scientific standard bites. Three separate questions must be answered:

1. **Is the logic trustworthy? (Golden Validation.)** You pick one completed Strategy Lab run as the "Validation Golden Run" and record one of three decisions with a required note: *engine agreement*, *reviewed deviations* or *manual override*. The comparison is frozen, and the database itself refuses any later edit or deletion of the review. A Golden record covers only its **exact setup** (program and version, ticker and every parameter); change any of these and you need a new record. Strategies that have never had a Golden run still fall back to an older strategy-wide human flag. See [ADR 0061](https://github.com/tim1016/learn-ai/blob/master/docs/architecture/adrs/0061-golden-validation-is-a-scoped-human-promotion-over-immutable-run-evidence.md).
2. **Is the code running the code that was tested? (Build proof.)** A qualification script runs each program's golden test corpus and, only if it passes, writes a **receipt** tying the code's fingerprint to its version. At every Start the running code is fingerprinted again; with no matching receipt, the bot does not start. See [ADR 0043](https://github.com/tim1016/learn-ai/blob/master/docs/architecture/adrs/0043-signal-program-build-proof-and-legacy-seal-migration.md).
3. **Were these exact settings covered by the evidence? (Corpus coverage.)** A bot may use a symbol or parameters that the golden test corpus never covered. On a paper account the run is allowed but stamped **exploratory** (not citable as proof); on a live account it is refused. There is no switch: the account type decides. See [ADR 0054](https://github.com/tim1016/learn-ai/blob/master/docs/architecture/adrs/0054-corpus-coverage-is-a-stamp-on-paper.md).

The proof is not checked once and forgotten: it is re-read and its files re-fingerprinted **at every Start and Resume**.

### Stage 4: deploy

- **You deploy at an account.** Accounts → pick the account → the **Deploy strategy** tab. An account is a place, and deploying is something you do on it ([ADR 0064](https://github.com/tim1016/learn-ai/blob/master/docs/architecture/adrs/0064-alpaca-navigation-is-account-first.md)).
- **The account decides the mode offered.** A paper account gets Paper. A live account that has not graduated gets **Shadow** (it reads the real account but places no real orders). A graduated live account gets Live. **Dry Run** (simulated fills) is always available.
- **The strategy–account pair must be approved.** Before a program may place orders on an account, you approve that exact pair on the Deploy page: review a plan, then confirm.
- **One Start decision.** You pick the program, the symbol, its parameters and the size (the default is one share). A single command deploys and starts the bot, behind the same Start decision you were shown as a preview. At Start the system checks that the evidence is fresh, the sealed account matches, the build is proven, validation is in place, market data is available, and the account is reconciled, not on hold and flat for that bot, with no open orders or unresolved work.
- **Nothing is ever edited in place.** A deployed bot's configuration is **sealed** and can never be changed; any change means a new bot. Each Start or Resume adds a new run record, and past runs are never overwritten ([ADR 0034](https://github.com/tim1016/learn-ai/blob/master/docs/architecture/adrs/0034-immutable-strategy-instances-append-only-runs.md)). The seal has two layers: the inner one fixes the program, its version and every parameter; the outer one adds the account, the mode, the trade plan, the size and the validation record used.
- **The clerk's record is what counts.** A launch counts only once the account's clerk has recorded it in its ledger. Any other file about the bot is evidence that gets repaired to match the clerk.

### Stage 5: trade

- **Each account trades inside its own clerk.** The clerk holds the ledger, the bot runner and a read-only IBKR price feed ([chapter 3](#3-account-lanes)).
- **Decisions use IBKR prices; orders go to Alpaca.** A running bot never reads the data lake.
- **The strategy only says "enter" or "exit".** At each finished bar the strategy emits a signal with no instrument and no account. A separate **trade plan** decides what to trade, and the clerk decides whether it may ([ADR 0012](https://github.com/tim1016/learn-ai/blob/master/docs/architecture/adrs/0012-strategy-as-signal-generator-action-plan-baseline.md), [ADR 0042](https://github.com/tim1016/learn-ai/blob/master/docs/architecture/adrs/0042-sealed-signal-and-account-scoped-custody-authorities.md)).
- **How an order reaches Alpaca.** The clerk checks the account's holds (and, on Live, arming and the cash limit), **writes its intent to the ledger before contacting Alpaca at all**, then sends the order. If a reply is lost, it asks Alpaca what happened; it never blindly sends again.

### Stage 6: evidence

- **The ledger.** Each account has one ledger file. Its heart is an append-only, **hash-chained** log: every entry is sealed to the one before it, so a missing or altered entry is detectable. Every "current state" table changes only together with a log entry, so the whole state can be rebuilt from the log ([ADR 0035](https://github.com/tim1016/learn-ai/blob/master/docs/architecture/adrs/0035-alpaca-clerk-sqlite-event-sourced-authority.md)).
- **Fills.** Each fill is recorded under the broker's own ID, corrections are appended rather than overwritten, profit and loss is computed first-in-first-out, and an unknown fee is shown as unknown, never as zero.
- **Per bot.** The sealed configuration, one record per run with its outcome and build proof, the decision receipts, and a ledger of the exact bars the bot saw, which lets a run be replayed and checked.
- **Research evidence** (backtest runs, parity verdicts, Golden reviews) lives in Python's research tables in Postgres.
- **"Flat" has one definition everywhere**, and every position a bot holds has an in-app path back to flat ("safe flatten"). Stopping a bot alone leaves its position open; that is deliberate, so a stop never trades by surprise ([ADR 0045](https://github.com/tim1016/learn-ai/blob/master/docs/architecture/adrs/0045-exposure-lifecycle-closure.md)).

---

## 3. Account lanes

*Checked against the code on 2026-09-18 (commit `a69b77d5`).*

> **In one sentence:** each brokerage account gets its own private lane (its own program, its own disk, its own keys) and a switchboard that holds no money routes your commands to the right lane through seven separate fences.

This is the deepest chapter, because lanes are where the architecture protects real money.

### 3.1 What a lane is

A **lane** is one brokerage account's private execution line: **one clerk program, its own disk (a "volume"), its own credentials, and exactly one Alpaca account.** Two accounts never share a program or a disk.

<figure class="md-diagram">
<svg viewBox="0 0 960 450" role="img" aria-label="Anatomy of two lanes. The coordinator at the top routes commands and holds no money. Below, the Live lane and the Paper lane each contain their own clerk program, their own keys and their own disk holding the account ledger, settings profile, bots and identity marker. A red dashed wall between them is labelled nothing shared between lanes. Each lane connects to exactly one Alpaca account.">
<defs><marker id="ah3" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path class="dg-arrowhead" d="M0,0 L10,5 L0,10 z"/></marker></defs>
<rect class="dg-box dg-coord" x="330" y="10" width="300" height="56" rx="6"/><text class="dg-h" x="480" y="33" text-anchor="middle">Coordinator</text><text class="dg-t" x="480" y="53" text-anchor="middle">routes commands · holds no money</text>
<path class="dg-flow dg-command" d="M420,66 L260,98" marker-end="url(#ah3)"/><path class="dg-flow dg-command" d="M540,66 L700,98" marker-end="url(#ah3)"/>
<rect class="dg-zone" x="30" y="100" width="420" height="250" rx="8"/><text class="dg-s" x="44" y="122">LIVE LANE</text>
<rect class="dg-box dg-live" x="50" y="138" width="180" height="64" rx="6"/><text class="dg-h" x="140" y="164" text-anchor="middle">Live clerk</text><text class="dg-t" x="140" y="186" text-anchor="middle">one program</text>
<rect class="dg-box" x="50" y="222" width="180" height="64" rx="6"/><text class="dg-h" x="140" y="248" text-anchor="middle">Its own keys</text><text class="dg-t" x="140" y="270" text-anchor="middle">Live credentials</text>
<rect class="dg-box dg-store" x="250" y="138" width="180" height="148" rx="6"/><text class="dg-h" x="340" y="162" text-anchor="middle">Its own disk</text><text class="dg-t" x="340" y="186" text-anchor="middle">account ledger</text><text class="dg-t" x="340" y="208" text-anchor="middle">settings profile</text><text class="dg-t" x="340" y="230" text-anchor="middle">bots and their runs</text><text class="dg-t" x="340" y="252" text-anchor="middle">identity marker</text>
<path class="dg-flow" d="M230,170 L248,170" marker-end="url(#ah3)"/>
<rect class="dg-zone" x="510" y="100" width="420" height="250" rx="8"/><text class="dg-s" x="524" y="122">PAPER LANE</text>
<rect class="dg-box dg-paper" x="530" y="138" width="180" height="64" rx="6"/><text class="dg-h" x="620" y="164" text-anchor="middle">Paper clerk</text><text class="dg-t" x="620" y="186" text-anchor="middle">one program</text>
<rect class="dg-box" x="530" y="222" width="180" height="64" rx="6"/><text class="dg-h" x="620" y="248" text-anchor="middle">Its own keys</text><text class="dg-t" x="620" y="270" text-anchor="middle">Paper credentials</text>
<rect class="dg-box dg-store" x="730" y="138" width="180" height="148" rx="6"/><text class="dg-h" x="820" y="162" text-anchor="middle">Its own disk</text><text class="dg-t" x="820" y="186" text-anchor="middle">account ledger</text><text class="dg-t" x="820" y="208" text-anchor="middle">settings profile</text><text class="dg-t" x="820" y="230" text-anchor="middle">bots and their runs</text><text class="dg-t" x="820" y="252" text-anchor="middle">identity marker</text>
<path class="dg-flow" d="M710,170 L728,170" marker-end="url(#ah3)"/>
<path class="dg-flow dg-blocked" d="M480,100 L480,350"/><text class="dg-label" x="480" y="372" text-anchor="middle">nothing shared between lanes</text>
<path class="dg-flow dg-money" d="M240,350 L240,386" marker-end="url(#ah3)"/><path class="dg-flow dg-money" d="M720,350 L720,386" marker-end="url(#ah3)"/>
<rect class="dg-box dg-outside" x="90" y="388" width="300" height="48" rx="6"/><text class="dg-h" x="240" y="417" text-anchor="middle">One Alpaca Live account</text>
<rect class="dg-box dg-outside" x="570" y="388" width="300" height="48" rx="6"/><text class="dg-h" x="720" y="417" text-anchor="middle">One Alpaca Paper account</text>
</svg>
<figcaption>A lane is a program, a disk, a set of keys and one account. Nothing crosses the wall between lanes.</figcaption>
</figure>

- **By design there are two lanes: Live and Paper.** More are possible, but each new one is a deliberate setup job on the Mac (see the [add-an-account runbook](https://github.com/tim1016/learn-ai/blob/master/docs/runbooks/add-an-alpaca-account.md)).
- **Why separate programs, not one program with two accounts?** The clerk code keeps program-wide state: the active account, its broker connection, its update streams. If two accounts shared one program they would share that state, and a fault on Paper could disable Live. Separate programs mean separate blast radius. This is the core decision of [ADR 0062](https://github.com/tim1016/learn-ai/blob/master/docs/architecture/adrs/0062-broker-clerk-fleet-control-plane.md).
- **How a lane knows its account.** A lane recognises its Alpaca account by the **account number**. You approve the account on the lane's Configuration page, which pins it. At every start the clerk reads the account back from Alpaca and **refuses to open its ledger if the number differs** from the pin.
- **Paper or live is never "unknown".** Each lane's paper/live label (`account_mode`) is set by configuration and then confirmed against the broker (paper account numbers have a recognisable shape). If the two disagree, the lane refuses to start.
- **One owner per account.** The coordinator enforces that only one lane may hold a given account, even if two disks were accidentally set up with the same credentials.

### 3.2 Three roles, one program

The coordinator and the clerks run the **same Python program**; its role setting decides what switches on.

| Role | What it contains | What it deliberately does *not* contain |
|---|---|---|
| **Coordinator** | Research and backtests, the data lake, long jobs, Polygon access, the fleet registry, and the public "door" that routes account commands. | Any Alpaca connection, any ledger, any bot runner, any IBKR connection. |
| **Clerk** (one per lane) | The disk check, the Alpaca connection, the account ledger, the update streams, its own read-only IBKR feed, the bot runner, and the account, bot and configuration pages' back end. | Research routes, the ability to write the data lake, any public door, a usable Polygon key. |
| **Combined** | Everything above in one program: the older posture. | The lane fences. |

**Why the coordinator has no broker connection.** It is a switchboard, not a trader. It stores no ledger, orders, positions or money, and it never adds up numbers across lanes. So a fault in the public-facing program cannot place an order or corrupt an account. The database that holds the lane directory is deliberately "custody-free", and a test checks it stays that way.

### 3.3 The five name tags

Every lane carries five identities. All are issued by the back office; the screen and callers never invent them. Each protects against a different accident.

| Name tag | What it is | What it prevents |
|---|---|---|
| **Clerk ID** | The lane's permanent name. | A command meant for one lane landing on another. Retired names are never reused. |
| **Volume ID** | A fingerprint written into a marker file on the lane's disk. | A copied, swapped or wrongly-mounted disk being opened as a lane. |
| **Worker key** | The lane's private ID, shown when it registers. It never crosses the public door. | A stray program registering as the lane. |
| **Routing epoch** | A counter that goes up every time the lane program starts and re-registers. | A retry or an open stream silently carrying over to a restarted program. |
| **Binding generation** | A counter that goes up only when the lane's settings, revision or account actually change. Every command carries the number it was prepared against. | A command prepared before an account switch being run against the new setup. It is refused instead. |

### 3.4 What keeps lanes apart

The lanes are separated by seven independent fences. A command has to get through every one of them.

<figure class="md-diagram">
<svg viewBox="0 0 960 510" role="img" aria-label="Seven fences, from the outside in: the network gives only the coordinator a door; the screen freezes its target; the coordinator checks the command envelope; the registry enforces its own rules in the database; the lane's doorman accepts changes only from the coordinator with matching identity; the disk gate proves the disk at start-up; and the account is fixed at start-up and is not part of any request.">
<defs><marker id="ah4" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path class="dg-arrowhead" d="M0,0 L10,5 L0,10 z"/></marker></defs>
<path class="dg-flow dg-command" d="M40,16 L40,494" marker-end="url(#ah4)"/><text class="dg-label" x="26" y="255" text-anchor="middle" transform="rotate(-90 26 255)">every command passes every layer</text>
<rect class="dg-box" x="70" y="16" width="880" height="60" rx="6"/><circle class="dg-box dg-coord" cx="100" cy="46" r="16"/><text class="dg-h" x="100" y="51" text-anchor="middle">1</text><text class="dg-h" x="130" y="42">Network: only the coordinator has a door</text><text class="dg-t" x="130" y="62">Lanes publish no ports and the web app's forwarder refuses to point at one, so a browser cannot reach a clerk directly.</text>
<rect class="dg-box" x="70" y="86" width="880" height="60" rx="6"/><circle class="dg-box dg-coord" cx="100" cy="116" r="16"/><text class="dg-h" x="100" y="121" text-anchor="middle">2</text><text class="dg-h" x="130" y="112">The screen freezes its target</text><text class="dg-t" x="130" y="132">The page address names the lane and account; opening an action freezes them, with the lane's version counter.</text>
<rect class="dg-box" x="70" y="156" width="880" height="60" rx="6"/><circle class="dg-box dg-coord" cx="100" cy="186" r="16"/><text class="dg-h" x="100" y="191" text-anchor="middle">3</text><text class="dg-h" x="130" y="182">The coordinator checks the envelope</text><text class="dg-t" x="130" y="202">Known operation, matching permission, one-time ticket, current version counter, URL account = the lane's account.</text>
<rect class="dg-box" x="70" y="226" width="880" height="60" rx="6"/><circle class="dg-box dg-coord" cx="100" cy="256" r="16"/><text class="dg-h" x="100" y="261" text-anchor="middle">4</text><text class="dg-h" x="130" y="252">The registry enforces its own rules</text><text class="dg-t" x="130" y="272">Names never change, rows are never deleted, counters only rise, one disk per lane: enforced by the database itself.</text>
<rect class="dg-box" x="70" y="296" width="880" height="60" rx="6"/><circle class="dg-box dg-coord" cx="100" cy="326" r="16"/><text class="dg-h" x="100" y="331" text-anchor="middle">5</text><text class="dg-h" x="130" y="322">The lane's doorman</text><text class="dg-t" x="130" y="342">A lane accepts a change only from the coordinator, and only if lane, restart number and version match what it serves.</text>
<rect class="dg-box" x="70" y="366" width="880" height="60" rx="6"/><circle class="dg-box dg-coord" cx="100" cy="396" r="16"/><text class="dg-h" x="100" y="401" text-anchor="middle">6</text><text class="dg-h" x="130" y="392">The disk gate at start-up</text><text class="dg-t" x="130" y="412">Before opening any ledger or broker link, the lane proves its disk is the registered one. A copied disk fails closed.</text>
<rect class="dg-box dg-good" x="70" y="436" width="880" height="60" rx="6"/><circle class="dg-box dg-coord" cx="100" cy="466" r="16"/><text class="dg-h" x="100" y="471" text-anchor="middle">7</text><text class="dg-h" x="130" y="462">The account is not in the request</text><text class="dg-t" x="130" y="482">Fixed at start-up. A request is only checked against it and can never change it; a bot sealed elsewhere is refused.</text>
</svg>
<figcaption>The seven fences. The innermost (green) is the strongest: the account a lane serves is decided when it starts, not by anything a request says.</figcaption>
</figure>

In plain words:

1. **The network.** Only the coordinator has a door. Lanes publish no ports, and the web app's forwarder refuses to point at a lane.
2. **The screen freezes its target.** The page address carries the broker, lane and account. When you open an action, the screen freezes that target together with the lane's binding generation. Clicking later never re-looks up "the current lane", and the app never remembers a "last account".
3. **The coordinator checks the envelope.** The operation must be one it knows, the permission must match, a one-time ticket must be present, the version counter must be current, and the account in the address must be the account assigned to that lane. The lane must also be alive and not retired.
4. **The registry enforces its own rules.** The coordinator's lane directory is a database with rules written into the database itself: names cannot change, rows cannot be deleted, a lane's life only moves forward, one disk root per lane, counters only rise, and a delivered receipt is final. Because the database enforces these, a bug or a race in the program cannot break them.
5. **The lane's doorman.** Every forwarded request carries the lane, restart number and version it was meant for. The lane compares them with what it actually serves and refuses a mismatched change before running it. A clerk also refuses any change that is not an authenticated forward from the coordinator, apart from two narrow, named exceptions.
6. **The disk gate at start-up.** Before a lane opens any ledger or broker connection, it proves its disk against the registry and checks that its bot files live inside that disk.
7. **The account is not in the request.** Below the coordinator, a lane never chooses which account to serve from anything in a request; the account is fixed when the lane starts. An account in an address is only *checked* against the lane's own account, and a mismatch is simply "not found". A bot sealed to a different account is refused.

> **The key insight:** a lane cannot be talked into serving the wrong account by a crafted request, because **the account is not in the request**. That is stronger than checking a parameter, because there is no parameter to get wrong.

One more rule protects against network trouble: **an assignment never expires.** If a lane goes silent it is marked unreachable, but it keeps its account. A network split therefore cannot create a second writer for the same account.

### 3.5 Following one click

Here is what really happens when you press **Resume** on a bot in the Live account's workspace. (For an existing bot the start-type button is labelled Resume; a new bot uses Deploy.)

<figure class="md-diagram">
<svg viewBox="0 0 960 630" role="img" aria-label="Sequence of one click. Your browser freezes the target and mints a one-time ticket, then sends the command with its envelope to the coordinator. The coordinator checks the envelope, finds the lane and account, writes a receipt and then forwards the command on the private link to the Live clerk. The clerk's doorman checks identity, runs start checks, registers the run and opens the bot's price feed, then answers. The coordinator settles the receipt as delivered, refused or unknown and the result appears on screen. Later, while the bot runs, the strategy says enter; the clerk runs cash and arming checks, saves its intent first, sends the order to Alpaca and receives fills. Live updates flow back to the browser, each stamped with the lane's identity.">
<defs><marker id="ah5" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path class="dg-arrowhead" d="M0,0 L10,5 L0,10 z"/></marker></defs>
<rect class="dg-box" x="30" y="10" width="160" height="40" rx="6"/><text class="dg-h" x="110" y="35" text-anchor="middle">Your browser</text>
<rect class="dg-box dg-coord" x="280" y="10" width="160" height="40" rx="6"/><text class="dg-h" x="360" y="35" text-anchor="middle">Coordinator</text>
<rect class="dg-box dg-live" x="530" y="10" width="160" height="40" rx="6"/><text class="dg-h" x="610" y="35" text-anchor="middle">Live clerk</text>
<rect class="dg-box dg-outside" x="770" y="10" width="160" height="40" rx="6"/><text class="dg-h" x="850" y="35" text-anchor="middle">Alpaca</text>
<path class="dg-zone" d="M110,50 L110,620"/><path class="dg-zone" d="M360,50 L360,620"/><path class="dg-zone" d="M610,50 L610,620"/><path class="dg-zone" d="M850,50 L850,620"/>
<rect class="dg-box" x="30" y="66" width="160" height="50" rx="6"/><text class="dg-t" x="110" y="87" text-anchor="middle">① freeze the target</text><text class="dg-t" x="110" y="106" text-anchor="middle">mint a one-time ticket</text>
<path class="dg-flow dg-command" d="M110,136 L356,136" marker-end="url(#ah5)"/><text class="dg-label" x="233" y="128" text-anchor="middle">② command + envelope</text>
<rect class="dg-box dg-coord" x="270" y="150" width="180" height="76" rx="6"/><text class="dg-t" x="360" y="172" text-anchor="middle">③ check the envelope</text><text class="dg-t" x="360" y="192" text-anchor="middle">find lane and account</text><text class="dg-t" x="360" y="212" text-anchor="middle">write receipt, then send</text>
<path class="dg-flow dg-command" d="M360,244 L606,244" marker-end="url(#ah5)"/><text class="dg-label" x="462" y="236">④ forward on private link</text>
<rect class="dg-box dg-live" x="520" y="258" width="180" height="76" rx="6"/><text class="dg-t" x="610" y="280" text-anchor="middle">⑤ doorman checks identity</text><text class="dg-t" x="610" y="300" text-anchor="middle">start checks, register run</text><text class="dg-t" x="610" y="320" text-anchor="middle">open the bot's price feed</text>
<path class="dg-flow dg-command" d="M610,356 L364,356" marker-end="url(#ah5)"/><text class="dg-label" x="485" y="348" text-anchor="middle">answer</text>
<rect class="dg-box dg-coord" x="270" y="370" width="180" height="56" rx="6"/><text class="dg-t" x="360" y="392" text-anchor="middle">⑥ settle the receipt</text><text class="dg-t" x="360" y="412" text-anchor="middle">delivered · refused · unknown</text>
<path class="dg-flow dg-command" d="M360,446 L114,446" marker-end="url(#ah5)"/><text class="dg-label" x="235" y="438" text-anchor="middle">result on screen</text>
<path class="dg-zone" d="M20,470 L940,470"/><text class="dg-label" x="480" y="474" text-anchor="middle">later, while the bot runs</text>
<rect class="dg-box dg-live" x="520" y="490" width="180" height="76" rx="6"/><text class="dg-t" x="610" y="512" text-anchor="middle">strategy says ENTER</text><text class="dg-t" x="610" y="532" text-anchor="middle">cash and arming checks</text><text class="dg-t" x="610" y="552" text-anchor="middle">intent saved first</text>
<path class="dg-flow dg-money" d="M700,510 L846,510" marker-end="url(#ah5)"/><text class="dg-label" x="773" y="502" text-anchor="middle">order</text>
<path class="dg-flow dg-data" d="M850,550 L704,550" marker-end="url(#ah5)"/><text class="dg-label" x="777" y="542" text-anchor="middle">fills</text>
<path class="dg-flow dg-data" d="M610,600 L114,600" marker-end="url(#ah5)"/><text class="dg-label" x="360" y="592" text-anchor="middle">live updates · each stamped with the lane's identity</text>
</svg>
<figcaption>One click, end to end. The coordinator writes its receipt before it sends anything, and only the clerk ever talks to Alpaca.</figcaption>
</figure>

1. **Your browser.** The bot's page address names the lane, the account and the bot. When the page opened, it froze the lane's binding generation. Your click creates a fresh **one-time ticket** (an idempotency key) and sends the command wrapped in an **envelope**: what kind of action it is, the ticket, the generation it expects, and the exact target.
2. **Coordinator: check the envelope.** The action must exist in the **operation catalog** (the one list of everything the screen may ask a clerk to do), its kind must match, the ticket must be present, and the envelope's target must equal the address.
3. **Coordinator: find the lane.** It checks, in order: the lane exists and belongs to this broker; it is not retired or draining; it has been heard from recently; it holds exactly one confirmed account; the generation matches; and the account in the address is that account.
4. **Coordinator: receipt first, then send.** Before sending anything it writes a **routing receipt** saying "not sent yet", then marks it sent (which cannot be undone), then forwards the command on the private link to that lane's approved address.
5. **The lane.** Its doorman proves the call came from the coordinator and that the identity matches. Then the real work: the bot runner checks the start conditions (market data healthy, ledger consistent, coverage, and arming on Live), the clerk registers the new run, and the bot starts receiving IBKR prices.
6. **The receipt is settled** as one of three outcomes:
   - **delivered**: the lane accepted it.
   - **refused**: the lane said no, with a reason.
   - **unknown**: it *may* have happened (a timeout, a crash mid-way). The rule is to reconcile by the ticket and **never resend blindly**. The clerk is the only judge of duplicates and outcomes.
7. **Later, while the bot runs.** When the strategy says "enter", the clerk runs its checks (holds, and on Live the arming and cash limits), **saves its intent to the ledger first**, then sends the order to Alpaca. Fill reports come back and are appended to the ledger.
8. **Live updates** flow back to your screen as a stream. The lane stamps its identity into every single message, and the coordinator closes the stream at the first message that doesn't match.

### 3.6 Where live prices come from

<figure class="md-diagram">
<svg viewBox="0 0 960 370" role="img" aria-label="Live prices: IBKR feeds IB Gateway on your Mac. IB Gateway gives each clerk its own read-only link. Inside each clerk one shared feed fans prices out to every bot; bots on the same symbol share one subscription. Alpaca provides accounts, orders, fills and its trading-hours clock, but never prices. A bot's chart history is fetched by the coordinator from the lake or Polygon, because clerks hold no Polygon key.">
<defs><marker id="ah6" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path class="dg-arrowhead" d="M0,0 L10,5 L0,10 z"/></marker></defs>
<rect class="dg-box dg-outside" x="30" y="20" width="160" height="50" rx="6"/><text class="dg-h" x="110" y="42" text-anchor="middle">IBKR</text><text class="dg-t" x="110" y="60" text-anchor="middle">live prices</text>
<path class="dg-flow dg-data" d="M110,70 L110,128" marker-end="url(#ah6)"/>
<rect class="dg-box" x="30" y="130" width="160" height="50" rx="6"/><text class="dg-h" x="110" y="152" text-anchor="middle">IB Gateway</text><text class="dg-t" x="110" y="170" text-anchor="middle">on your Mac</text>
<text class="dg-s" x="110" y="204" text-anchor="middle">one read-only link</text><text class="dg-s" x="110" y="220" text-anchor="middle">per clerk</text>
<path class="dg-flow dg-data" d="M190,146 L230,146 L230,84 L288,84" marker-end="url(#ah6)"/>
<path class="dg-flow dg-data" d="M190,164 L250,164 L250,264 L288,264" marker-end="url(#ah6)"/>
<rect class="dg-zone" x="270" y="16" width="360" height="160" rx="8"/><text class="dg-s" x="282" y="36">LIVE CLERK</text>
<rect class="dg-box dg-live" x="290" y="56" width="130" height="56" rx="6"/><text class="dg-h" x="355" y="80" text-anchor="middle">shared feed</text><text class="dg-t" x="355" y="100" text-anchor="middle">one per clerk</text>
<rect class="dg-box" x="470" y="44" width="140" height="28" rx="4"/><text class="dg-t" x="540" y="63" text-anchor="middle">bot · symbol A</text>
<rect class="dg-box" x="470" y="84" width="140" height="28" rx="4"/><text class="dg-t" x="540" y="103" text-anchor="middle">bot · symbol A</text>
<rect class="dg-box" x="470" y="124" width="140" height="28" rx="4"/><text class="dg-t" x="540" y="143" text-anchor="middle">bot · symbol B</text>
<path class="dg-flow dg-data" d="M420,76 L468,58" marker-end="url(#ah6)"/><path class="dg-flow dg-data" d="M420,84 L468,98" marker-end="url(#ah6)"/><path class="dg-flow dg-data" d="M420,94 L468,138" marker-end="url(#ah6)"/>
<text class="dg-s" x="282" y="168">same symbol, one subscription</text>
<rect class="dg-zone" x="270" y="196" width="360" height="160" rx="8"/><text class="dg-s" x="282" y="216">PAPER CLERK</text>
<rect class="dg-box dg-paper" x="290" y="236" width="130" height="56" rx="6"/><text class="dg-h" x="355" y="260" text-anchor="middle">shared feed</text><text class="dg-t" x="355" y="280" text-anchor="middle">one per clerk</text>
<rect class="dg-box" x="470" y="224" width="140" height="28" rx="4"/><text class="dg-t" x="540" y="243" text-anchor="middle">bot · symbol C</text>
<rect class="dg-box" x="470" y="264" width="140" height="28" rx="4"/><text class="dg-t" x="540" y="283" text-anchor="middle">bot · symbol A</text>
<rect class="dg-box" x="470" y="304" width="140" height="28" rx="4"/><text class="dg-t" x="540" y="323" text-anchor="middle">bot · symbol D</text>
<path class="dg-flow dg-data" d="M420,256 L468,238" marker-end="url(#ah6)"/><path class="dg-flow dg-data" d="M420,264 L468,278" marker-end="url(#ah6)"/><path class="dg-flow dg-data" d="M420,274 L468,318" marker-end="url(#ah6)"/>
<rect class="dg-box dg-outside" x="680" y="16" width="250" height="160" rx="6"/><text class="dg-h" x="805" y="44" text-anchor="middle">Alpaca provides</text><text class="dg-t" x="805" y="70" text-anchor="middle">accounts, orders, fills</text><text class="dg-t" x="805" y="92" text-anchor="middle">its trading-hours clock</text><text class="dg-t" x="805" y="126" text-anchor="middle">never prices:</text><text class="dg-t" x="805" y="146" text-anchor="middle">owner decision, 2026-09-16</text>
<rect class="dg-box dg-coord" x="680" y="196" width="250" height="160" rx="6"/><text class="dg-h" x="805" y="222" text-anchor="middle">Coordinator</text><text class="dg-t" x="805" y="248" text-anchor="middle">a bot's chart history</text><text class="dg-t" x="805" y="270" text-anchor="middle">is fetched here, from</text><text class="dg-t" x="805" y="292" text-anchor="middle">the lake or Polygon</text><text class="dg-t" x="805" y="324" text-anchor="middle">clerks hold no Polygon key</text>
<path class="dg-flow dg-data" d="M678,276 L632,276" marker-end="url(#ah6)"/>
<path class="dg-flow dg-data" d="M678,236 L655,236 L655,120 L632,120" marker-end="url(#ah6)"/>
</svg>
<figcaption>IBKR supplies every lane's live prices through one Gateway; each clerk has its own read-only link and shares one feed among its bots.</figcaption>
</figure>

- **IBKR is the live price source for every lane.** Alpaca supplies accounts, its trading-hours clock, orders and fill reports, and nothing else. Buying Alpaca market data is **prohibited by owner decision** (2026-09-16) because of its extra cost; the code that would open an Alpaca data stream refuses to run. See ADR 0062's [provider decision](https://github.com/tim1016/learn-ai/blob/master/docs/architecture/adrs/0062-broker-clerk-fleet-control-plane.md#retained-market-data-provider--owner-decision-2026-09-16).
- **Each clerk has its own read-only connection** to IB Gateway. The coordinator has none.
- **Inside a clerk, one shared feed fans prices out to every bot**, and bots watching the same symbol share one subscription.
- **"Tradable" needs positive evidence.** A stock counts as tradable only with a recent sign from IBKR, either a "not halted" signal or a very recent trade; a connected socket alone is not enough. Without that evidence, no new positions are opened.
- **When IB Gateway is down**, bots cannot start, and a running bot stops if prices do not arrive before its next decision (the stop is recorded with a typed `FEED_DEATH` reason). Some runs are allowed to wait out a short reconnect and stitch the missing minute back together ([ADR 0053](https://github.com/tim1016/learn-ai/blob/master/docs/architecture/adrs/0053-feed-continuity-same-run-recovery.md)).
- **Chart history for a bot** takes one round trip: your screen asks the coordinator; the coordinator forwards to the lane; the lane makes one call back to the coordinator, which fetches the history (from the lake or Polygon); the lane adds its own fill markers and answers. Chart reads use a separate queue so they can never block commands.

### 3.7 Inside a clerk

<figure class="md-diagram">
<svg viewBox="0 0 980 320" role="img" aria-label="Inside a clerk. Top row, at start-up in this order: prove the disk and register; lock and open the settings profile; reserve the account with the coordinator before opening the ledger; choose authority as Paper, Shadow or Live, or report that activation is needed; confirm the binding and write the evidence to its own disk; recover first, then start streams, the price feed, the bot runner and sweeps. Bottom row, every order in this order: the strategy signal says only enter or exit; account holds are checked; on Live, arming, the cash bound and the daily loss hold are checked; the intent is saved to the ledger; the order goes to Alpaca; fills are appended and the state is rebuilt from the ledger.">
<defs><marker id="ah7" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path class="dg-arrowhead" d="M0,0 L10,5 L0,10 z"/></marker></defs>
<text class="dg-s" x="10" y="24">AT START-UP, IN THIS ORDER</text>
<rect class="dg-box" x="10" y="34" width="148" height="74" rx="6"/><text class="dg-h" x="84" y="56" text-anchor="middle">① Prove the disk</text><text class="dg-t" x="84" y="76" text-anchor="middle">then register with</text><text class="dg-t" x="84" y="94" text-anchor="middle">a new restart number</text>
<rect class="dg-box" x="172" y="34" width="148" height="74" rx="6"/><text class="dg-h" x="246" y="56" text-anchor="middle">② Lock and open</text><text class="dg-t" x="246" y="76" text-anchor="middle">the settings</text><text class="dg-t" x="246" y="94" text-anchor="middle">profile</text>
<rect class="dg-box" x="334" y="34" width="148" height="74" rx="6"/><text class="dg-h" x="408" y="56" text-anchor="middle">③ Reserve account</text><text class="dg-t" x="408" y="76" text-anchor="middle">with the coordinator,</text><text class="dg-t" x="408" y="94" text-anchor="middle">before the ledger</text>
<rect class="dg-box" x="496" y="34" width="148" height="74" rx="6"/><text class="dg-h" x="570" y="56" text-anchor="middle">④ Choose authority</text><text class="dg-t" x="570" y="76" text-anchor="middle">Paper · Shadow · Live</text><text class="dg-t" x="570" y="94" text-anchor="middle">or needs activation</text>
<rect class="dg-box" x="658" y="34" width="148" height="74" rx="6"/><text class="dg-h" x="732" y="56" text-anchor="middle">⑤ Confirm binding</text><text class="dg-t" x="732" y="76" text-anchor="middle">write the evidence</text><text class="dg-t" x="732" y="94" text-anchor="middle">to its own disk</text>
<rect class="dg-box dg-good" x="820" y="34" width="148" height="74" rx="6"/><text class="dg-h" x="894" y="56" text-anchor="middle">⑥ Recover first</text><text class="dg-t" x="894" y="76" text-anchor="middle">then streams, feed,</text><text class="dg-t" x="894" y="94" text-anchor="middle">bots and sweeps</text>
<path class="dg-flow" d="M158,71 L170,71" marker-end="url(#ah7)"/><path class="dg-flow" d="M320,71 L332,71" marker-end="url(#ah7)"/><path class="dg-flow" d="M482,71 L494,71" marker-end="url(#ah7)"/><path class="dg-flow" d="M644,71 L656,71" marker-end="url(#ah7)"/><path class="dg-flow" d="M806,71 L818,71" marker-end="url(#ah7)"/>
<text class="dg-s" x="10" y="160">EVERY ORDER, IN THIS ORDER</text>
<rect class="dg-box" x="10" y="170" width="148" height="74" rx="6"/><text class="dg-h" x="84" y="192" text-anchor="middle">Strategy signal</text><text class="dg-t" x="84" y="212" text-anchor="middle">enter or exit only;</text><text class="dg-t" x="84" y="230" text-anchor="middle">no account, no size</text>
<rect class="dg-box" x="172" y="170" width="148" height="74" rx="6"/><text class="dg-h" x="246" y="192" text-anchor="middle">Account holds?</text><text class="dg-t" x="246" y="212" text-anchor="middle">a drift or stuck exit</text><text class="dg-t" x="246" y="230" text-anchor="middle">blocks new entries</text>
<rect class="dg-box dg-live" x="334" y="170" width="148" height="74" rx="6"/><text class="dg-h" x="408" y="192" text-anchor="middle">Live only</text><text class="dg-t" x="408" y="212" text-anchor="middle">armed? within cash?</text><text class="dg-t" x="408" y="230" text-anchor="middle">daily loss hold?</text>
<rect class="dg-box dg-store" x="496" y="170" width="148" height="74" rx="6"/><text class="dg-h" x="570" y="192" text-anchor="middle">Intent saved</text><text class="dg-t" x="570" y="212" text-anchor="middle">to the ledger</text><text class="dg-t" x="570" y="230" text-anchor="middle">before sending</text>
<rect class="dg-box" x="658" y="170" width="148" height="74" rx="6"/><text class="dg-h" x="732" y="192" text-anchor="middle">Order to Alpaca</text><text class="dg-t" x="732" y="212" text-anchor="middle">lost reply? ask, and</text><text class="dg-t" x="732" y="230" text-anchor="middle">never resend blindly</text>
<rect class="dg-box dg-store" x="820" y="170" width="148" height="74" rx="6"/><text class="dg-h" x="894" y="192" text-anchor="middle">Fills appended</text><text class="dg-t" x="894" y="212" text-anchor="middle">state rebuilt</text><text class="dg-t" x="894" y="230" text-anchor="middle">from the ledger</text>
<path class="dg-flow" d="M158,207 L170,207" marker-end="url(#ah7)"/><path class="dg-flow" d="M320,207 L332,207" marker-end="url(#ah7)"/><path class="dg-flow" d="M482,207 L494,207" marker-end="url(#ah7)"/><path class="dg-flow dg-money" d="M644,207 L656,207" marker-end="url(#ah7)"/><path class="dg-flow dg-data" d="M806,207 L818,207" marker-end="url(#ah7)"/>
<text class="dg-s" x="10" y="284">Exits are never blocked by the Live money limits: a limit can stop a bot from buying, never from getting out.</text>
</svg>
<figcaption>A clerk's two fixed sequences: how it starts, and how every order travels.</figcaption>
</figure>

- **One record keeper per account, or none.** Each account has exactly one custody authority: the activated clerk ledger. There is no fallback to an older store. Without it, the account simply cannot trade ([ADR 0037](https://github.com/tim1016/learn-ai/blob/master/docs/architecture/adrs/0037-sqlite-sole-alpaca-custody-authority.md)).
- **The ledger is tamper-evident.** Every event is appended to a hash-chained log, current state is always rebuilt ("folded") from that log and never edited directly, and intents are saved before any broker call.
- **It starts in a fixed order** (top row of the diagram). The account is reserved with the coordinator *before* the ledger opens, and recovery runs *before* any bot may start. Starts stay refused while any earlier order's outcome is uncertain.
- **If the coordinator is unreachable at start-up**, a lane may still start, but only with exactly the account binding it previously confirmed, as recorded on its own disk. A brand-new lane, or a lane whose binding changed, waits for the coordinator.
- **Settings are per lane** ([ADR 0060](https://github.com/tim1016/learn-ai/blob/master/docs/architecture/adrs/0060-broker-configuration-is-a-user-owned-profile-on-the-clerk-volume.md)). A lane's settings profile lives on its own disk; secrets come only from environment files, never from the profile. Staging a change does nothing; **Apply** records the intent; restarting that one lane makes it take effect. A crash always reboots the last effective revision. The Configuration page stays reachable even when a lane's account binding is broken, so it can be repaired.
- **Reconciliation.** After recovery, a periodic sweep compares what Alpaca says the account holds with what the ledger says. Any difference pauses new positions ([chapter 5](#5-single-sources-of-truth) shows this loop).

### 3.8 Paper, Shadow and real money

| | **Paper** | **Live Shadow** | **Real-money Live** |
|---|---|---|---|
| Places real orders? | Practice orders at Alpaca Paper | No: it reads the real Live account but only simulates fills | Yes |
| What makes the lane this mode | A Paper activation record (without it the lane reports `ACTIVATION_REQUIRED` and cannot trade) | A Live account with no Live activation yet | Three independent agreements: Live mode configured, a Live activation record, and the broker confirming the account on its Live endpoint |
| How you get here | Activate the Paper lane | Approve the Live account | The **graduation** ceremony on the Live account's Configuration page |
| Per-bot arming | Not needed | Not needed | Required: granted per bot from the Mac's command line, sealed to that bot, and it lapses after a set number of trading sessions |
| Money limits | None beyond the account | None (no real orders) | Every new buy is bounded by the cash the broker reports; a daily loss limit puts the whole account on hold. **Exits are never blocked.** |
| Uncovered parameters | Allowed, stamped "exploratory" | Refused | Refused |

**Graduation** is a three-step ceremony (status → plan → apply). The lane gathers its own fresh broker evidence, requires a flat account with no open orders, makes a verified backup, shows you a plan to confirm, and then restarts itself as real-money Live. **Graduation arms nothing and places no order**; arming each bot is a separate, deliberate act. Shadow rehearsal is optional, not a requirement. The rules are in [ADR 0059](https://github.com/tim1016/learn-ai/blob/master/docs/architecture/adrs/0059-real-money-live-behind-shadow-gate-arming-and-cash-bound-envelope.md).

### 3.9 A lane's life

<figure class="md-diagram">
<svg viewBox="0 0 960 250" role="img" aria-label="A lane's life. What the registry stores: provisioned, then draining, which is decided but not built, then retired, which is final and whose name is never reused. What the screen shows, worked out fresh each time: starting, ready, degraded, unreachable. Ready means reachable with a confirmed account, not necessarily able to trade.">
<defs><marker id="ah9" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path class="dg-arrowhead" d="M0,0 L10,5 L0,10 z"/></marker></defs>
<text class="dg-s" x="20" y="24">WHAT THE REGISTRY STORES (DURABLE)</text>
<rect class="dg-box" x="20" y="36" width="230" height="56" rx="6"/><text class="dg-h" x="135" y="60" text-anchor="middle">Provisioned</text><text class="dg-t" x="135" y="80" text-anchor="middle">enrolled by a host ceremony</text>
<rect class="dg-box dg-outside" x="310" y="36" width="280" height="56" rx="6"/><text class="dg-h" x="450" y="60" text-anchor="middle">Draining</text><text class="dg-t" x="450" y="80" text-anchor="middle">decided (ADR 0063) · not built</text>
<rect class="dg-box" x="650" y="36" width="230" height="56" rx="6"/><text class="dg-h" x="765" y="60" text-anchor="middle">Retired</text><text class="dg-t" x="765" y="80" text-anchor="middle">final · name never reused</text>
<path class="dg-flow dg-blocked" d="M250,64 L308,64" marker-end="url(#ah9)"/><path class="dg-flow dg-blocked" d="M590,64 L648,64" marker-end="url(#ah9)"/>
<text class="dg-s" x="20" y="114">Today a lane that has ever run cannot be retired; you disable it instead (stop it, keep its disk and its name).</text>
<text class="dg-s" x="20" y="148">WHAT THE SCREEN SHOWS (WORKED OUT FRESH EACH TIME)</text>
<rect class="dg-box" x="20" y="160" width="200" height="50" rx="6"/><text class="dg-h" x="120" y="182" text-anchor="middle">Starting</text><text class="dg-t" x="120" y="200" text-anchor="middle">heard from, not bound</text>
<rect class="dg-box dg-good" x="240" y="160" width="200" height="50" rx="6"/><text class="dg-h" x="340" y="182" text-anchor="middle">Ready</text><text class="dg-t" x="340" y="200" text-anchor="middle">account confirmed</text>
<rect class="dg-box dg-live" x="460" y="160" width="200" height="50" rx="6"/><text class="dg-h" x="560" y="182" text-anchor="middle">Degraded</text><text class="dg-t" x="560" y="200" text-anchor="middle">partly working</text>
<rect class="dg-box dg-weak" x="680" y="160" width="200" height="50" rx="6"/><text class="dg-h" x="780" y="182" text-anchor="middle">Unreachable</text><text class="dg-t" x="780" y="200" text-anchor="middle">silent · keeps its account</text>
<text class="dg-s" x="20" y="236">"Ready" means reachable with a confirmed account. It does not promise the lane can trade (see chapter 7).</text>
</svg>
<figcaption>The registry stores only three durable states; everything the screen shows about a lane's health is worked out fresh each time.</figcaption>
</figure>

- **Enrolment happens on the Mac, never from the browser.** A command-line ceremony creates a fresh disk; mints the lane's name, worker key and two service tokens; writes the disk's identity marker; and approves the lane's internal address. The secrets go into environment files that git ignores. Then you bind the account through the lane's Configuration page ([add-an-account runbook](https://github.com/tim1016/learn-ai/blob/master/docs/runbooks/add-an-alpaca-account.md)).
- **A lane that needs activation still boots**, so you can see why it cannot trade.
- **Draining and handover were decided but are not built** ([ADR 0063](https://github.com/tim1016/learn-ai/blob/master/docs/architecture/adrs/0063-draining-is-an-observed-lane-handover.md)): draining will close a lane to new work but never force it flat. Until it exists, a lane that has ever run cannot be permanently retired or handed to another lane, which is one of the weak spots in [chapter 7](#7-weak-spots-built-into-the-design).
- **Retirement is final.** A retired lane is never deleted or revived, and its names are never reused.

### 3.10 How the screen mirrors the lanes

- **The account is the place** ([ADR 0064](https://github.com/tim1016/learn-ai/blob/master/docs/architecture/adrs/0064-alpaca-navigation-is-account-first.md)). Each account has one workspace: a header (name, Paper or Live, equity, sync) and tabs for Overview, Bots, Gallery, Configuration and Deploy strategy. **Accounts** is the only page that lists every account, and switching accounts keeps you on the same tab.
- **The workspace lives at the lane.** Configuration and "not ready" explanations work even before an account is bound. A bad or not-ready link explains itself where it is; it never quietly redirects you to another lane.
- **Badges and switchers only navigate.** The account switcher and the top-bar Live/Paper badges take you places; they never change what a command targets. A command always carries the account it was opened against. The full account number appears only on Configuration and in the confirmation for consequential actions.
- **The directory.** The screen's list of lanes (identity, health, version counters, nickname) comes from the coordinator. Every page takes the account's name from it, and each lane loads, fails and retries on its own, so one broken lane never blanks the others.

---

## 4. The seams

*Checked against the code on 2026-09-18 (commit `a69b77d5`).*

> **In one sentence:** a seam is any place where one part hands work to another, and the strongest seams are the ones whose contract is generated from code and checked by the build on every change.

Seams are where bugs hide: each side can be correct on its own while the two disagree about what crosses between them. So for every seam this chapter asks three questions: **what crosses it, what contract governs it, and does a machine check that contract?**

<figure class="md-diagram">
<svg viewBox="0 0 960 540" role="img" aria-label="The seams, coloured by how strongly each contract is enforced. Green seams are generated and checked by the build: the web app to .NET GraphQL, the web app to the coordinator, and the coordinator to the clerks. Amber seams are hand-written but tested: .NET to the coordinator, the services to Postgres and Redis, the clerks to their ledgers and the clerks to Alpaca. Grey dashed seams are convention or a vendor's protocol: the web app to .NET jobs, IB Gateway to the clerks and Polygon to the coordinator.">
<defs><marker id="ah10" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path class="dg-arrowhead" d="M0,0 L10,5 L0,10 z"/></marker></defs>
<rect class="dg-box" x="20" y="20" width="180" height="56" rx="6"/><text class="dg-h" x="110" y="44" text-anchor="middle">Web app</text><text class="dg-t" x="110" y="64" text-anchor="middle">in your browser</text>
<rect class="dg-box" x="20" y="230" width="180" height="56" rx="6"/><text class="dg-h" x="110" y="254" text-anchor="middle">.NET backend</text><text class="dg-t" x="110" y="274" text-anchor="middle">older service</text>
<rect class="dg-box dg-coord" x="380" y="120" width="200" height="70" rx="6"/><text class="dg-h" x="480" y="150" text-anchor="middle">Coordinator</text><text class="dg-t" x="480" y="172" text-anchor="middle">Python</text>
<rect class="dg-box dg-live" x="740" y="120" width="200" height="70" rx="6"/><text class="dg-h" x="840" y="150" text-anchor="middle">Clerks</text><text class="dg-t" x="840" y="172" text-anchor="middle">Live and Paper</text>
<rect class="dg-box dg-outside" x="400" y="20" width="160" height="50" rx="6"/><text class="dg-h" x="480" y="50" text-anchor="middle">Polygon</text>
<rect class="dg-box dg-outside" x="740" y="20" width="200" height="50" rx="6"/><text class="dg-h" x="840" y="50" text-anchor="middle">IB Gateway</text>
<rect class="dg-box dg-store" x="740" y="300" width="150" height="56" rx="6"/><text class="dg-h" x="815" y="324" text-anchor="middle">Clerk ledgers</text><text class="dg-t" x="815" y="344" text-anchor="middle">on each lane's disk</text>
<rect class="dg-box dg-outside" x="740" y="410" width="200" height="50" rx="6"/><text class="dg-h" x="840" y="440" text-anchor="middle">Alpaca</text>
<rect class="dg-box dg-store" x="20" y="410" width="400" height="56" rx="6"/><text class="dg-h" x="220" y="434" text-anchor="middle">Postgres · Redis</text><text class="dg-t" x="220" y="454" text-anchor="middle">shared records · job-progress scratch pad</text>
<path class="dg-flow dg-checked" d="M80,76 L80,228" marker-end="url(#ah10)"/><text class="dg-label" x="88" y="150">①</text>
<path class="dg-flow dg-convention" d="M140,76 L140,228" marker-end="url(#ah10)"/><text class="dg-label" x="148" y="190">②</text>
<path class="dg-flow dg-checked" d="M200,48 L290,48 L290,140 L378,140" marker-end="url(#ah10)"/><text class="dg-label" x="298" y="100">③</text>
<path class="dg-flow dg-tested" d="M200,258 L290,258 L290,170 L378,170" marker-end="url(#ah10)"/><text class="dg-label" x="298" y="222">④</text>
<path class="dg-flow dg-tested" d="M110,286 L110,408" marker-end="url(#ah10)"/><text class="dg-label" x="118" y="350">⑤</text>
<path class="dg-flow dg-tested" d="M480,190 L480,438 L422,438" marker-end="url(#ah10)"/><text class="dg-label" x="488" y="320">⑤</text>
<path class="dg-flow dg-checked" d="M580,155 L738,155" marker-end="url(#ah10)"/><text class="dg-label" x="659" y="147" text-anchor="middle">⑥</text>
<path class="dg-flow dg-tested" d="M790,190 L790,298" marker-end="url(#ah10)"/><text class="dg-label" x="798" y="250">⑦</text>
<path class="dg-flow dg-tested" d="M915,190 L915,408" marker-end="url(#ah10)"/><text class="dg-label" x="923" y="300">⑧</text>
<path class="dg-flow dg-convention" d="M840,70 L840,118" marker-end="url(#ah10)"/><text class="dg-label" x="848" y="100">⑨</text>
<path class="dg-flow dg-convention" d="M480,70 L480,118" marker-end="url(#ah10)"/><text class="dg-label" x="488" y="100">⑩</text>
<path class="dg-flow dg-checked" d="M20,508 L70,508"/><text class="dg-t" x="80" y="512">generated and checked by the build</text>
<path class="dg-flow dg-tested" d="M330,508 L380,508"/><text class="dg-t" x="390" y="512">hand-written, covered by tests</text>
<path class="dg-flow dg-convention" d="M620,508 L670,508"/><text class="dg-t" x="680" y="512">convention or a vendor's protocol</text>
</svg>
<figcaption>Seam numbers match the table below. Green is the strongest guarantee; grey dashed means nothing but discipline keeps the two sides in step.</figcaption>
</figure>

| # | Seam | What crosses it | The contract | Checked by a machine? |
|---|---|---|---|---|
| ① | Web app → .NET (GraphQL) | Market-data explorer, Data Lab sessions, the pricing lab, the practice portfolio, research experiments | A GraphQL schema, exported from .NET's code to a committed file | **Yes.** The build fails if the schema changes without the file. The web app's queries are hand-written; a test checks their inputs against the schema. |
| ② | Web app → .NET (jobs) | "Start this long job", then a live progress stream | The job routes in .NET's code | No snapshot; convention only. |
| ③ | Web app → coordinator (`/api`) | Everything else: research, the data lake, and all account and bot commands | An OpenAPI file generated from Python's code; the web app's types are regenerated from it | **Yes.** The build fails if the file or the regenerated types drift. The list of routes that need the shared secret lives in one file used by the forwarder, the web app and a Python test. |
| ④ | .NET → coordinator | Relayed market-data and math requests, and long jobs handed over | Python's REST routes; .NET's copies of the shapes are hand-written | Partly: one shared sample payload is tested on both sides. |
| ⑤ | Services → Postgres and Redis | Records; job status and progress | .NET's database migrations for its tables; Python's own versioned schema for its tables (Python's are named `research_*`); a key layout for Redis described in comments on both sides | Partly: the lake catalog's layout is checked against a real Postgres nightly. The Redis layout is convention only. |
| ⑥ | Coordinator → clerks | Every account command, stream and chart request | The **operation catalog**, plus a private internal link with one secret token per direction | **Yes.** The catalog is exported to two snapshot files (Python and web app) that the build regenerates and compares; a test checks that every path it names is served by the clerk. The internal link is deliberately left out of the published API. |
| ⑦ | Clerk → its ledgers | Every custody event; the settings profile | The ledger's schema and version | Yes, at run time: each ledger's schema version is checked when it opens. |
| ⑧ | Clerk → Alpaca | Orders out; fills and account facts back | Alpaca's own API | We record every raw reply before reading it, then convert it into our own broker types ([ADR 0032](https://github.com/tim1016/learn-ai/blob/master/docs/architecture/adrs/0032-broker-contract-v2-and-verbatim-capture.md)). |
| ⑨ | IB Gateway → clerk | Live bars and halt status, read-only | IBKR's protocol | Vendor's contract. |
| ⑩ | Polygon → coordinator | Price history and reference data | Polygon's API | Vendor's contract; history is fingerprinted as it lands in the lake. |

Three smaller seams complete the picture. Python calls back to .NET for exactly one thing, launching LEAN comparison runs. The coordinator asks the **LEAN launcher** on the Mac to start a LEAN container, which reads the data lake directly. And a few research screens call Python's port directly, skipping the forwarder, which limits them to routes that need no secret.

**What this means for you.** When an agent changes something that crosses seams ①, ③ or ⑥, the build will catch a mismatch. Seams ②, ④ and ⑤ depend on tests and discipline, so ask for a check that both sides were updated. The contract list and its history are in [ADR 0031](https://github.com/tim1016/learn-ai/blob/master/docs/architecture/adrs/0031-cross-stack-boundary-selection-and-contract-generation.md). ADR 0031 still describes generating the web app's GraphQL code; that generator has since been removed, and the code is the authority.

---

## 5. Single sources of truth

*Checked against the code on 2026-09-18 (commit `a69b77d5`).*

> **In one sentence:** every important question has exactly one place that answers it, and wherever possible a machine check stops a second copy from creeping in.

This is the repo's standing rule: **one canonical implementation per concept.** A duplicate is allowed only for a real reason (speed, or keeping a layer self-contained), and then it must carry a **parity test** proving it gives the same answer as the original, naming that original.

### The truth table

| Question | The one place that answers it | What stops a second copy |
|---|---|---|
| What is the market's schedule (trading days, open, close, half-days)? | One calendar module, built on a pinned version of the NYSE calendar library | A build check rejects any other calendar, any hard-coded 9:30 or 16:00, and unsafe time functions ([ADR 0022](https://github.com/tim1016/learn-ai/blob/master/docs/architecture/adrs/0022-temporal-authority-calendar-and-timestamp.md)) |
| How is a moment in time written down? | Whole milliseconds since 1 January 1970, UTC, everywhere: in memory, on disk and between services | The same build check. Conversion happens only when data arrives and when a screen displays it, through one shared display component. |
| Is the market trading *right now* (halts)? | The live IBKR feed | The calendar deliberately does not answer this; it cannot see halts |
| What is the right formula for X? | One canonical implementation per concept, named in the [math sources-of-truth registry](https://github.com/tim1016/learn-ai/blob/master/docs/math-sources-of-truth.md) | Copies need a parity test; stored reference answers ("golden fixtures") are checked on every change. The provenance note on each formula is enforced by review, not by a machine. |
| Which past prices did a backtest read? | The data lake's fingerprinted files; Postgres only indexes them | The old second store was removed, and a test forbids a new one. Known exception: a few research tools still read Polygon directly ([ADR 0049](https://github.com/tim1016/learn-ai/blob/master/docs/architecture/adrs/0049-data-lake-is-the-market-data-authority.md)). |
| Which live prices does a bot see? | IBKR, through each clerk's read-only feed | The code that would open an Alpaca data stream refuses to run (owner decision) |
| What does this account own, and which orders are in flight? | That account's clerk ledger: one file, hash-chained | No fallback store; unique keys block duplicate commands; only one writer per account may contact the broker |
| What actually happened at the broker (fills, fees)? | Alpaca is the record, and the ledger is reconciled against it | A disagreement raises a hold (see the diagram below); neither side is silently overwritten |
| Which lanes exist, and which account is where? | The fleet registry on the coordinator | Rules inside the database; one owner per account; assignments never expire |
| What can the screen ask a clerk to do, and at which address? | The operation catalog | The router, the published API and the screen's address builder are all generated from it, and the build compares the two copies |
| What exactly do the services send each other? | The committed contract files (OpenAPI for Python, the GraphQL schema for .NET) | The build regenerates them and fails on drift |
| Which settings is this lane using? | The settings profile on that lane's disk; secrets only from environment files | A test checks that the environment is read only at start-up |
| Which engine owns a job, and who owns research results? | The [engine authority map](https://github.com/tim1016/learn-ai/blob/master/docs/architecture/engine-authority-map.md); research tables are declared and written only by Python | A protected document; retirement tests stop the old .NET study screens from returning |
| Which status words and refusal reasons can the screen show? | Defined once in Python, including their wording | Generated snapshot copies for the screen, compared by the build |
| Which pages exist? | One menu list in the web app | The menu bar and every page title are drawn from it |
| What was decided, what do the words mean, and what is broken? | ADRs; [CONTEXT.md](https://github.com/tim1016/learn-ai/blob/master/CONTEXT.md); [known-gaps.md](https://github.com/tim1016/learn-ai/blob/master/docs/known-gaps.md); and [doc-authority.md](https://github.com/tim1016/learn-ai/blob/master/docs/doc-authority.md) for which document wins | A build check on every ADR's status line, and a documentation test that classifies every document and checks its links |

Even this manual follows the rule: the copy the app shows is compared with the file in `docs/` on every change, and the build fails if they differ.

### When the ledger and the broker disagree

<figure class="md-diagram">
<svg viewBox="0 0 960 260" role="img" aria-label="Reconciliation. Alpaca's reported positions and the clerk ledger's assigned positions are compared on every sweep. If they are exactly equal, trading continues. If there is any difference, a position-drift hold pauses new positions account-wide, still allows exits and risk reduction, and lifts only when an exact match is proven.">
<defs><marker id="ah11" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path class="dg-arrowhead" d="M0,0 L10,5 L0,10 z"/></marker></defs>
<rect class="dg-box dg-outside" x="40" y="20" width="260" height="60" rx="6"/><text class="dg-h" x="170" y="44" text-anchor="middle">Alpaca says</text><text class="dg-t" x="170" y="66" text-anchor="middle">the positions it holds</text>
<rect class="dg-box" x="380" y="20" width="200" height="60" rx="6"/><text class="dg-h" x="480" y="44" text-anchor="middle">Compare</text><text class="dg-t" x="480" y="66" text-anchor="middle">on every sweep</text>
<rect class="dg-box dg-store" x="660" y="20" width="260" height="60" rx="6"/><text class="dg-h" x="790" y="44" text-anchor="middle">The ledger says</text><text class="dg-t" x="790" y="66" text-anchor="middle">positions assigned to bots</text>
<path class="dg-flow dg-data" d="M300,50 L378,50" marker-end="url(#ah11)"/><path class="dg-flow dg-data" d="M660,50 L582,50" marker-end="url(#ah11)"/>
<path class="dg-flow" d="M440,80 L300,148" marker-end="url(#ah11)"/><path class="dg-flow" d="M520,80 L700,148" marker-end="url(#ah11)"/>
<rect class="dg-box dg-good" x="100" y="150" width="320" height="90" rx="6"/><text class="dg-h" x="260" y="182" text-anchor="middle">Exactly equal</text><text class="dg-t" x="260" y="206" text-anchor="middle">clean: trading continues</text>
<rect class="dg-box dg-weak" x="520" y="150" width="400" height="90" rx="6"/><text class="dg-h" x="720" y="174" text-anchor="middle">Any difference: position-drift hold</text><text class="dg-t" x="720" y="196" text-anchor="middle">new positions paused, account-wide</text><text class="dg-t" x="720" y="214" text-anchor="middle">exits and risk reduction still allowed</text><text class="dg-t" x="720" y="232" text-anchor="middle">lifts only when an exact match is proven</text>
</svg>
<figcaption>The system never quietly picks a winner. A difference, even one that an order still working might explain, pauses new risk until the two agree exactly.</figcaption>
</figure>

Orders the clerk did not place are flagged as "unexplained". For what happened at the broker, Alpaca is the record: its equity curve is the account's official curve, and the fees it actually charged are the truth (the fee model is only a prediction).

### Time: three separate questions

<figure class="md-diagram">
<svg viewBox="0 0 960 160" role="img" aria-label="Time has three separate authorities. The calendar answers the schedule, past and future: trading days, open, close and half-days, from one module guarded by the build. The live feed answers whether a stock is trading right now, including halts, from IBKR per clerk. Every time value is stored as milliseconds since 1970 in UTC and converted only when data arrives and when a screen shows it.">
<rect class="dg-box" x="20" y="16" width="290" height="126" rx="6"/><text class="dg-h" x="165" y="42" text-anchor="middle">The calendar</text><text class="dg-t" x="165" y="66" text-anchor="middle">the schedule, past and future</text><text class="dg-t" x="165" y="88" text-anchor="middle">trading days · open · close</text><text class="dg-t" x="165" y="110" text-anchor="middle">half-days</text><text class="dg-s" x="165" y="132" text-anchor="middle">one module, guarded by the build</text>
<rect class="dg-box" x="335" y="16" width="290" height="126" rx="6"/><text class="dg-h" x="480" y="42" text-anchor="middle">The live feed</text><text class="dg-t" x="480" y="66" text-anchor="middle">is it trading right now?</text><text class="dg-t" x="480" y="88" text-anchor="middle">halts and pauses</text><text class="dg-s" x="480" y="132" text-anchor="middle">IBKR, through each clerk</text>
<rect class="dg-box dg-store" x="650" y="16" width="290" height="126" rx="6"/><text class="dg-h" x="795" y="42" text-anchor="middle">Every time value</text><text class="dg-t" x="795" y="66" text-anchor="middle">milliseconds since 1970, UTC</text><text class="dg-t" x="795" y="88" text-anchor="middle">converted only on arrival</text><text class="dg-t" x="795" y="110" text-anchor="middle">and on screen</text><text class="dg-s" x="795" y="132" text-anchor="middle">never text, never local time</text>
</svg>
<figcaption>The calendar cannot see a halt, and the live feed cannot see next month's holidays. Each answers only its own question.</figcaption>
</figure>

### Where the rule rests on review, not a machine

Honesty matters here, because a rule nobody checks can quietly stop holding. Three rules are enforced only by review today:

- the provenance note on each formula (its source, its reference and its canonical file);
- the rule that the screen must pass backend codes through the shared label formatter and never rewrite backend-written sentences;
- the rule that the screen never decides for itself whether a position is flat.

---

## 6. Who decides what

*Checked against the code on 2026-09-18 (commit `a69b77d5`).*

> **In one sentence:** Python computes and decides, the screens only show what they are told, .NET keeps a few older records and runs jobs, and you make the decisions that matter, some on screen and some only on the Mac.

<figure class="md-diagram">
<svg viewBox="0 0 960 400" role="img" aria-label="Who decides what. Three layers: the web app shows the verdicts, wording and allowed actions it is given and freezes its target; .NET stores older records and runs jobs with no broker and no custody; Python computes and decides, through the engines and lake, the coordinator which routes, fences and keeps receipts but never trades, and the clerks which decide admission, deduplicate, keep custody and send orders. Verdicts and wording flow up only. On the right, you decide: approve an account, validate a strategy, approve a pairing, deploy, start and stop, graduate Live; and from the Mac's command line: enrol a lane, arm a bot, retire and restore.">
<defs><marker id="ah13" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path class="dg-arrowhead" d="M0,0 L10,5 L0,10 z"/></marker></defs>
<rect class="dg-box" x="20" y="20" width="720" height="70" rx="6"/><text class="dg-h" x="380" y="48" text-anchor="middle">Web app: shows</text><text class="dg-t" x="380" y="72" text-anchor="middle">renders the verdicts, wording and allowed actions it is given · freezes its target</text>
<rect class="dg-box" x="20" y="110" width="720" height="70" rx="6"/><text class="dg-h" x="380" y="138" text-anchor="middle">.NET: keeps older records, runs jobs</text><text class="dg-t" x="380" y="162" text-anchor="middle">Data Lab sessions · practice portfolio · job hand-off · no broker, no custody</text>
<rect class="dg-zone" x="20" y="200" width="720" height="180" rx="8"/><text class="dg-s" x="34" y="222">PYTHON: COMPUTES AND DECIDES</text>
<rect class="dg-box" x="36" y="236" width="220" height="128" rx="6"/><text class="dg-h" x="146" y="262" text-anchor="middle">Engines and lake</text><text class="dg-t" x="146" y="288" text-anchor="middle">backtests, math,</text><text class="dg-t" x="146" y="308" text-anchor="middle">price history</text>
<rect class="dg-box dg-coord" x="270" y="236" width="220" height="128" rx="6"/><text class="dg-h" x="380" y="262" text-anchor="middle">Coordinator</text><text class="dg-t" x="380" y="288" text-anchor="middle">routes, fences,</text><text class="dg-t" x="380" y="308" text-anchor="middle">keeps receipts,</text><text class="dg-t" x="380" y="328" text-anchor="middle">never trades</text>
<rect class="dg-box dg-live" x="504" y="236" width="220" height="128" rx="6"/><text class="dg-h" x="614" y="262" text-anchor="middle">Clerks</text><text class="dg-t" x="614" y="288" text-anchor="middle">decide admission,</text><text class="dg-t" x="614" y="308" text-anchor="middle">deduplicate, keep</text><text class="dg-t" x="614" y="328" text-anchor="middle">custody, send orders</text>
<path class="dg-flow dg-data" d="M770,370 L770,26" marker-end="url(#ah13)"/><text class="dg-label" x="760" y="200" text-anchor="middle" transform="rotate(-90 760 200)">verdicts and wording flow up only</text>
<rect class="dg-box dg-good" x="800" y="20" width="140" height="360" rx="6"/><text class="dg-h" x="870" y="46" text-anchor="middle">You decide</text><text class="dg-s" x="870" y="70" text-anchor="middle">on screen:</text><text class="dg-t" x="870" y="92" text-anchor="middle">approve account</text><text class="dg-t" x="870" y="114" text-anchor="middle">validate strategy</text><text class="dg-t" x="870" y="136" text-anchor="middle">approve pairing</text><text class="dg-t" x="870" y="158" text-anchor="middle">deploy, start, stop</text><text class="dg-t" x="870" y="180" text-anchor="middle">graduate Live</text><text class="dg-s" x="870" y="222" text-anchor="middle">on the Mac only:</text><text class="dg-t" x="870" y="244" text-anchor="middle">enrol a lane</text><text class="dg-t" x="870" y="266" text-anchor="middle">arm a bot</text><text class="dg-t" x="870" y="288" text-anchor="middle">retire, restore</text>
</svg>
<figcaption>Judgment lives in Python and in you. The screens carry it; they never create it.</figcaption>
</figure>

### The rules, in plain words

- **The screen never works out a verdict.** Whether an action is allowed, which action is the main one, why a button is disabled and whether data is fresh are all written by Python and only displayed by the web app. The original decision for this ([ADR 0013](https://github.com/tim1016/learn-ai/blob/master/docs/architecture/adrs/0013-operator-surface-judgment-vs-evidence.md)) has been superseded, but its principle lives on in the Alpaca-era decisions ([ADR 0035](https://github.com/tim1016/learn-ai/blob/master/docs/architecture/adrs/0035-alpaca-clerk-sqlite-event-sourced-authority.md): "the frontend derives no safety"; [ADR 0059](https://github.com/tim1016/learn-ai/blob/master/docs/architecture/adrs/0059-real-money-live-behind-shadow-gate-arming-and-cash-bound-envelope.md): the frontend "renders it and never composes it").
- **Wording is written once, on the server.** Every sentence you read about your accounts comes from a server template, so every screen says the same thing. The screen may tidy up a code name for display, but it must never rewrite a server-written sentence or alter an audit ID.
- **"Flat" has one definition.** One small rule in Python decides whether a position counts as open. Nothing else, including the screen, re-decides it ([ADR 0036](https://github.com/tim1016/learn-ai/blob/master/docs/architecture/adrs/0036-single-flatness-boundary-backend-owned.md)).
- **One bot control system.** Alpaca's clerk is the only bot control system; the older IBKR one is retired. For a bot's state, the clerk's database wins over any side file. Two facts are deliberately kept in plain files so that **a stopped bot stays stopped even when its clerk is down** ([ADR 0038](https://github.com/tim1016/learn-ai/blob/master/docs/architecture/adrs/0038-alpaca-sole-bot-control-plane.md)).
- **The strategy only says when.** The strategy says "enter now" or "exit now". The trade plan says *what* to trade. The clerk decides *whether* and places the order. A strategy never selects an account, submits an order or claims custody.
- **You pick the sizing choice; Python works out the shares** ([ADR 0009](https://github.com/tim1016/learn-ai/blob/master/docs/architecture/adrs/0009-live-sizing-authority-and-provenance.md)). On Live, the cash limit applies on top.
- **The coordinator is the switchboard; each clerk is the accountant.** The coordinator decides *which lane* gets a request and refuses mismatches. The clerk decides admission, spots duplicates, keeps custody and records outcomes. The coordinator never computes profit or exposure across lanes, and there is no fleet-wide Start, Stop or Flatten button.
- **Some decisions are deliberately kept off the web.** Enrolling a lane, arming a bot for real money, and retiring or restoring are ceremonies run from the Mac's command line, never from a browser. That keeps the most consequential actions behind physical access to the machine.

### The three layers

| Layer | Its role | What it must never do |
|---|---|---|
| **Web app** (Angular) | Display; freeze the target of an action from the page you opened | Invent a verdict, compose safety wording, decide flatness, choose "the current account" at click time |
| **.NET** | Older records (Data Lab sessions, the practice portfolio), the long-job API, and the layout of the tables it owns | Touch a broker or custody; add new tables (the owner is phasing it out) |
| **Python** | Math, engines, the data lake, the coordinator, the clerks, and every operator verdict and sentence | Break the one-answer rule in chapter 5 |

---

## 7. Weak spots built into the design

*Checked against the code on 2026-09-18 (commit `a69b77d5`).*

> **In one sentence:** the strongest part of the design is inside each lane, and the weaker parts are around it: the single machine, the single price source, the single door, and a few jobs that are decided but not yet built.

This chapter lists weaknesses that come from **how the system is built**, not individual bugs. Each links to where it is tracked, so this page does not go stale when one is fixed. Open defects in general live in [known-gaps.md](https://github.com/tim1016/learn-ai/blob/master/docs/known-gaps.md).

<figure class="md-diagram">
<svg viewBox="0 0 960 480" role="img" aria-label="Weak spots shaded over the lanes. Strong, in green: each clerk's sealed account, ledger, cash bound and exits that are never refused, and the registry's database rules. Weaker, in red: the coordinator as the only door, and IB Gateway as the single live price source that logs out. The whole Mac is marked as the development machine that is also the live-money machine, and the web app relies on one shared secret. Listed below: lane retirement and handover are blind to orders; nightly fault tests run a different layout; ready does not mean able to trade; outside regular hours emergency exits wait for the open.">
<defs><marker id="ah14" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path class="dg-arrowhead" d="M0,0 L10,5 L0,10 z"/></marker></defs>
<rect class="dg-zone" x="10" y="10" width="940" height="460" rx="10"/>
<text class="dg-s" x="24" y="34">YOUR MAC: THE DEVELOPMENT MACHINE IS ALSO THE LIVE-MONEY MACHINE ①</text>
<rect class="dg-box" x="30" y="60" width="180" height="60" rx="6"/><text class="dg-h" x="120" y="84" text-anchor="middle">Web app</text><text class="dg-t" x="120" y="106" text-anchor="middle">one shared secret ⑤</text>
<rect class="dg-box dg-weak" x="280" y="60" width="200" height="60" rx="6"/><text class="dg-h" x="380" y="84" text-anchor="middle">Coordinator</text><text class="dg-t" x="380" y="106" text-anchor="middle">the only door ③</text>
<rect class="dg-box dg-good" x="280" y="160" width="200" height="60" rx="6"/><text class="dg-h" x="380" y="184" text-anchor="middle">Registry</text><text class="dg-t" x="380" y="206" text-anchor="middle">rules in the database</text>
<rect class="dg-box dg-good" x="560" y="60" width="170" height="124" rx="6"/><text class="dg-h" x="645" y="84" text-anchor="middle">Live clerk</text><text class="dg-t" x="645" y="108" text-anchor="middle">sealed account</text><text class="dg-t" x="645" y="128" text-anchor="middle">tamper-evident ledger</text><text class="dg-t" x="645" y="148" text-anchor="middle">cash bound</text><text class="dg-t" x="645" y="168" text-anchor="middle">exits never refused</text>
<rect class="dg-box dg-good" x="760" y="60" width="170" height="124" rx="6"/><text class="dg-h" x="845" y="84" text-anchor="middle">Paper clerk</text><text class="dg-t" x="845" y="108" text-anchor="middle">sealed account</text><text class="dg-t" x="845" y="128" text-anchor="middle">tamper-evident ledger</text><text class="dg-t" x="845" y="168" text-anchor="middle">exits never refused</text>
<rect class="dg-box dg-weak" x="560" y="250" width="370" height="60" rx="6"/><text class="dg-h" x="745" y="274" text-anchor="middle">IB Gateway: one live price source ②</text><text class="dg-t" x="745" y="296" text-anchor="middle">logs out nightly unless set to restart · weekly re-login</text>
<path class="dg-flow dg-command" d="M210,90 L278,90" marker-end="url(#ah14)"/>
<path class="dg-flow dg-command" d="M480,90 L558,90" marker-end="url(#ah14)"/>
<path class="dg-flow" d="M380,120 L380,158" marker-end="url(#ah14)"/>
<path class="dg-flow dg-data" d="M645,250 L645,186" marker-end="url(#ah14)"/><path class="dg-flow dg-data" d="M845,250 L845,186" marker-end="url(#ah14)"/>
<path class="dg-flow dg-blocked" d="M30,354 L90,354" marker-end="url(#ah14)"/><text class="dg-t" x="100" y="358">④ retiring or handing over a lane is blind to the lane's orders (decided, not built)</text>
<text class="dg-t" x="30" y="390">⑥ the nightly fault tests run the reviewed layout, not the one running on this Mac</text>
<text class="dg-t" x="30" y="418">⑦ "ready" means reachable with a confirmed account, not "able to trade"</text>
<text class="dg-t" x="30" y="446">⑧ outside regular hours, emergency and operator exits wait for the market to open</text>
</svg>
<figcaption>Green is strong, red is weaker. The numbers match the list below, which runs from most to least important.</figcaption>
</figure>

### ① The development machine is also the live-money machine

- **What could happen.** Real-money trading runs on the same Mac, in the same code folder and container setup, where code is written and AI agents work. The Live clerk runs whatever code is in that folder the next time it restarts, including an automatic restart after a crash. A routine full restart restarts the Live lane too. Credentials and backups sit on the same machine.
- **Why the design allows it.** It is a one-machine setup by design. A stricter, reviewed production layout exists in the repo but is not the one running.
- **What already limits the damage.** Each lane has its own disk with an identity check before it opens. Every port listens on this machine only. A committed, build-checked layout exists that the Mac can move onto.
- **Tracked in** [#2151](https://github.com/tim1016/learn-ai/issues/2151) (move to a separate machine), [#2166](https://github.com/tim1016/learn-ai/issues/2166) and [#2105](https://github.com/tim1016/learn-ai/issues/2105).

### ② One live price source, and it logs itself out

- **What could happen.** Every bot on both lanes makes decisions from one IB Gateway program on the Mac. It logs out nightly unless its own settings restart it, and IBKR forces a weekly re-login regardless. While it is down, bots cannot start and running bots stop at their next decision. The same feed is also how the system spots trading halts.
- **Why the design allows it.** It is an explicit owner decision: no paid Alpaca data fallback, and an outage never switches one on ([ADR 0062](https://github.com/tim1016/learn-ai/blob/master/docs/architecture/adrs/0062-broker-clerk-fleet-control-plane.md#retained-market-data-provider--owner-decision-2026-09-16)). Decisions are made on IBKR prices while orders fill at Alpaca, two different venues.
- **What already limits the damage.** It fails closed. A missing feed refuses new runs, a halt stays in force until IBKR says otherwise, every feed-loss stop carries a typed reason, some runs can ride out a short reconnect, and a position left behind can still be closed through safe flatten.
- **Tracked in** the ADR 0062 decision above (accepted, not open). Setup steps are in the [IBKR setup guide](https://github.com/tim1016/learn-ai/blob/master/docs/runbooks/ibkr-setup-guide.md).

### ③ The coordinator is the only door, and lanes keep trading when it closes

- **What could happen.** The browser reaches both lanes only through the coordinator. If it goes down, bots keep trading on their own, but Stop, Deploy and every other button stop working until it returns. The only control left is at the Mac's command line.
- **Why the design allows it.** It is intended: a lane's liveness never depends on the switchboard, and a coordinator outage never hands an account to a second writer ([ADR 0062](https://github.com/tim1016/learn-ai/blob/master/docs/architecture/adrs/0062-broker-clerk-fleet-control-plane.md)).
- **What already limits the damage.** The money limits live inside each lane (cash bound, daily loss hold, exits never refused), and the Mac can always stop a lane's container.
- **Tracked in** ADR 0062 (by design; no open issue).

### ④ The control plane cannot see orders, so a lane cannot yet be safely retired or handed over

- **What could happen.** Bots place orders from inside their own lane, and the coordinator never sees them. So nothing central can prove a lane is quiet before it is retired or its account is given to another lane. Until the planned drain is built, those operations are gated by host ceremonies that cannot prove quiet.
- **Why the design allows it.** The coordinator's blindness to orders is deliberate: it holds no custody. The consequence is recorded in [ADR 0063](https://github.com/tim1016/learn-ai/blob/master/docs/architecture/adrs/0063-draining-is-an-observed-lane-handover.md), which designs an observed drain.
- **What already limits the damage.** These are host-only ceremonies with no browser path, a lost heartbeat never releases an account, and each lane only ever serves the account sealed into it. Today the practice is to *disable* a lane, never retire it.
- **Tracked in** known-gaps.md ("Fleet account retirement"), plus [#2154](https://github.com/tim1016/learn-ai/issues/2154), [#2155](https://github.com/tim1016/learn-ai/issues/2155) and [#2157](https://github.com/tim1016/learn-ai/issues/2157).

### ⑤ No personal login: one shared secret

- **What could happen.** There are no user accounts and no record of *who* did what. Anyone who can open the app in a browser on this Mac acts as the operator, including for real-money actions.
- **Why the design allows it.** A single-operator machine; the decision on real authentication is deliberately deferred in [ADR 0062](https://github.com/tim1016/learn-ai/blob/master/docs/architecture/adrs/0062-broker-clerk-fleet-control-plane.md).
- **What already limits the damage.** Ports are bound to this machine only, the most consequential actions are Mac-only ceremonies, Live trading needs per-bot arming that expires, and lanes refuse changes that did not come through the coordinator.
- **Tracked in** ADR 0062 (a deferred owner decision).

### ⑥ Tests cover the reviewed layout, not the running one

- **What could happen.** The nightly fault tests start a fresh copy of the reviewed production layout on a build server. This Mac runs a looser development layout plus private local settings, and that combination is never fault-tested. Backup and restore have not been rehearsed on the real machine.
- **Why the design allows it.** The deployed layout and the reviewed layout are two different files, and moving onto the reviewed one has not happened yet.
- **What already limits the damage.** The registry's database rules are well tested, and the build does check the committed development layout's structure.
- **Tracked in** [#2070](https://github.com/tim1016/learn-ai/issues/2070).

### ⑦ "Ready" does not mean "able to trade"

- **What could happen.** A lane shows **ready** once it is reachable and its account is confirmed, even if it cannot place real orders. For example, a Live lane in Shadow mode is "ready".
- **Why the design allows it.** Whether "ready" should mean "addressable" or "able to trade" is an open owner decision recorded in the [fleet authority document](https://github.com/tim1016/learn-ai/blob/master/docs/broker-clerk-fleet-authority.md).
- **What already limits the damage.** Each lane has its own server-written live-verdict badge (paper, live and armed, or live and unarmed), and Live is always identified on screen.
- **Tracked in** the fleet authority document (no open issue).

### ⑧ Outside regular hours, emergency exits wait for the open

- **What could happen.** Bots may hold positions in extended hours. But emergency and operator exits (safe flatten, the stuck-exit watchdog, reconciliation) are sent as market orders, which Alpaca holds until the regular open. An exit planned for the last minutes of extended trading can carry the position overnight.
- **Why the design allows it.** An exit with no deciding strategy gets the regular-session market shape. Manual orders are paper-only, so on Live the remaining remedy is Alpaca's own dashboard.
- **What already limits the damage.** Exits that a strategy decides are sent as marketable limit orders in extended hours, and an unfilled exit is raised as an uncertainty, never silently repriced.
- **Tracked in** [#2007](https://github.com/tim1016/learn-ai/issues/2007).

### Smaller design-level notes

- **One process per lane does everything.** Bots, reconciliation and screen reads share one program per lane, so heavy screen reads can slow a lane. Paper and Live are at least separate. See known-gaps.md.
- **The default posture runs unfenced.** A fresh copy of the repo starts in the combined posture, without lane fences; the normal restart script loads the fleet posture, and the program now warns if an enrolled disk has lost its fleet settings.
- **The money limits are deliberately narrow.** The only limits are the cash bound and the account-wide daily loss hold: no per-order cap and no symbol allow-list ([ADR 0059](https://github.com/tim1016/learn-ai/blob/master/docs/architecture/adrs/0059-real-money-live-behind-shadow-gate-arming-and-cash-bound-envelope.md)). That is a chosen risk appetite, not a gap.

---

## 8. Glossary

*The full, authoritative definitions are in [CONTEXT.md](https://github.com/tim1016/learn-ai/blob/master/CONTEXT.md). These are one-line reminders.*

| Term | Plain meaning |
|---|---|
| **Account mode** (`account_mode`) | Whether an account is paper or live. Configured, then confirmed with the broker; never "unknown". |
| **`ACTIVATION_REQUIRED`** | A lane that has booted but has no activation record yet, so it can be looked at but cannot trade. |
| **ADR** | Architecture Decision Record: a short document recording one decision and why. The authority for "what did we decide". |
| **Arming** | Per-bot permission to place real-money entries, granted from the Mac's command line, sealed to that bot, and lapsing after a set number of sessions. |
| **Binding generation** | A lane's version counter; it rises only when its settings, revision or account change. Commands carry the one they expect. |
| **Build proof** (build receipt) | A record tying a program's code fingerprint to a passing run of its golden tests. No match at Start, no start. |
| **Canonical** | The one official implementation or document for a concept. |
| **Clerk** (clerk agent) | The program that owns one brokerage account: its ledger, its bots, its broker connection. |
| **Combined posture** | The older mode where one Python program does every job, without lane fences. |
| **Command envelope** | The wrapper every account command carries: what kind it is, a one-time ticket, the expected version, and the target. |
| **Coordinator** (fleet coordinator) | The Python program that does research and history and routes every account command. It holds no money. |
| **Corpus coverage** | Whether a bot's exact symbol and settings were covered by the golden test evidence. Paper: stamped "exploratory". Live: required. |
| **Custody** | Knowing and recording what an account owns and which orders are in flight. Owned by the clerk's ledger. |
| **Data lake** | The folder of fingerprinted price-history files. One writer: the coordinator. |
| **Data plane** | Another name for the Python coordinator's research and data role. |
| **Directory** | The screen's list of lanes and their health, supplied by the coordinator. |
| **Draining** | Closing a lane to new work before handover. Decided (ADR 0063), not yet built. |
| **Epoch** (routing epoch) | A counter that rises every time a lane program restarts and re-registers. |
| **Fence** | Any check that keeps one lane's work from reaching another. |
| **Fleet** | The coordinator plus all the lanes, working as one system. |
| **Fleet registry** | The coordinator's database of which lanes exist and which account each holds. No money in it. |
| **Fold** | Rebuilding current state from the ledger's log, instead of editing it directly. |
| **Golden fixture** | A stored reference answer used to prove a calculation matches its source. |
| **Golden Validation** | Your reviewed, frozen decision that a specific strategy setup is trustworthy. |
| **Graduation** | The ceremony that turns the Live lane from Shadow into real-money Live. Arms nothing, places no order. |
| **Hash chain** | A log where each entry is sealed to the one before it, so tampering is detectable. |
| **IB Gateway** | IBKR's program on the Mac through which live prices arrive. |
| **Idempotency key** (one-time ticket) | A unique ticket on each command so it can be recognised if it arrives twice. |
| **Lane** | One account's private execution line: a clerk, its disk, its keys and one Alpaca account. |
| **Ledger** (clerk ledger) | The account's tamper-evident record, the single custody authority. |
| **Live Shadow** | The Live lane before graduation: it reads the real account but only simulates fills. |
| **Operation catalog** | The one list of everything the screen may ask a clerk to do, and at which address. |
| **`outcome_unknown`** | A command that may or may not have happened. Reconcile by its ticket; never resend blindly. |
| **Parity test** | A test proving a duplicate implementation gives the same answer as the canonical one. |
| **Position-drift hold** | The account-wide pause on new positions when the ledger and Alpaca disagree. |
| **Profile** (settings profile) | A lane's user-owned broker settings, stored on its own disk. Staged, applied, then restart. |
| **Reconciliation** | Comparing the ledger with what the broker reports. |
| **Role** | The setting that decides whether the Python program runs as coordinator, clerk or combined. |
| **Routing receipt** | The coordinator's record of one command's delivery, written before sending. |
| **Seal** | Freezing a bot's configuration so it can never be edited; a change means a new bot. |
| **Seam** | Any place where one part hands work to another. |
| **Signal program** | A registered, deployable strategy that only emits "enter" or "exit". |
| **Single source of truth** | The one place that answers a given question. |
| **Volume** | A lane's own disk, with an identity marker. |
| **Worker key** | A lane's private registration ID; never crosses the public door. |

---

## Where to go next

- **The lanes in full technical depth:** [ADR 0062](https://github.com/tim1016/learn-ai/blob/master/docs/architecture/adrs/0062-broker-clerk-fleet-control-plane.md) (the decision) and the [broker clerk fleet authority](https://github.com/tim1016/learn-ai/blob/master/docs/broker-clerk-fleet-authority.md) (how it was built, with evidence; parts of it predate recent fixes, and the code is the authority).
- **Running the lanes:** the [two-lane posture runbook](https://github.com/tim1016/learn-ai/blob/master/docs/runbooks/fleet-dev-two-lane-posture.md) and the [add-an-account runbook](https://github.com/tim1016/learn-ai/blob/master/docs/runbooks/add-an-alpaca-account.md).
- **Real money:** [ADR 0059](https://github.com/tim1016/learn-ai/blob/master/docs/architecture/adrs/0059-real-money-live-behind-shadow-gate-arming-and-cash-bound-envelope.md).
- **Which document wins:** [doc-authority.md](https://github.com/tim1016/learn-ai/blob/master/docs/doc-authority.md).
- **Words:** [CONTEXT.md](https://github.com/tim1016/learn-ai/blob/master/CONTEXT.md).
- **What is broken right now:** [known-gaps.md](https://github.com/tim1016/learn-ai/blob/master/docs/known-gaps.md).
