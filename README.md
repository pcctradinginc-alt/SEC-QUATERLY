# SEC 13F Signal Engine

Automated quarterly engine that answers one question:

> **Which 10 stocks currently show the strongest combination of high-conviction
> institutional accumulation and confirming SEC insider activity, and which Call
> option best expresses each signal?**

It tracks 52 institutional 13F filers, compares their latest holdings with prior
quarters, cross-checks SEC Form 4 insider purchases since the quarter-end, ranks
every name with a **deterministic 0–100 SIGNAL SCORE**, selects one liquid Call
per stock under predefined filters (or reports `NO_SUITABLE_OPTION_FOUND`), and
e-mails a responsive, minimalist HTML report.

Runs automatically on **16 May / 16 Aug / 16 Nov / 16 Feb** (next business day
if weekend) via GitHub Actions.

---

## Pipeline

```
date_check.py         is today the run date?
fetch_filings.py      SEC EDGAR 13F-HR XML for all 52 CIKs, CUSIP→ticker (OpenFIGI + SEC map), split check
parse_13f.py          quarter-over-quarter deltas (NEW / ADD / REDUCE / EXIT), weights, rank tiers
scoring.py            per-filer alpha components, manager quality, crowding proxy, sell signals
signal_engine.py      ★ deterministic SIGNAL SCORE (8 factors) + Form 4 insider look-up → Top 10
options_lookup.py     Tradier Call chains → option_selector.py → one Call or NO_SUITABLE_OPTION_FOUND
explain_signals.py    commentary via llm_router.py (Haiku → Sonnet → Opus cascade, cached)
backtest.py           90/180-day performance of past Top-10s
send_report.py        responsive HTML e-mail via Gmail
```

`python src/run_pipeline.py` runs everything; `--from <step>`, `--no-email`,
`--skip-insider`, `--skip-options` and `--date YYYY-MM-DD` are supported.

---

## The SIGNAL SCORE (0–100, deterministic)

Each factor is a **fixed 0–100 transform** (absolute anchors, not universe
percentiles), so a stock's score does not change because a different set of
peers happened to be scored. Weights sum to 100 (`config.SIGNAL_WEIGHTS`):

| Factor | Weight | What it measures |
|---|---:|---|
| NEW / ADD activity | 15 | strength of the best buyer's action (new position size band or add %) plus breadth |
| Portfolio conviction | 15 | largest buyer weight (10 % of a book = 100), rank tier (Top 3/5/10), outsized vs. the manager's median |
| Manager quality | 15 | dynamic 0–1 quality of the buyers (concentration + turnover, blended with a prior) |
| Multi-quarter accumulation | 10 | consecutive build quarters (current + history), silent-build bonus |
| Smart-money consensus | 15 | number of independent buyers, scaled by their average quality |
| Insider buying (Form 4) | 15 | open-market purchases since the 13F quarter-end: value (40), distinct buyers and cluster buying (25), seniority of the buyers (25), size against the buyer's own stake (10); only *discretionary* selling subtracts |
| Filing freshness | 10 | `exp(-k · turnover · filing delay)` and the age of the filing |
| Institutional crowding | 5 | inverse of the crowding proxy (hedge-fund-hotel list + oversized cluster) |

**Confluence bonus (max +10).** A weighted sum rates "excellent 13F, no insider"
the same as "average 13F, excellent insider". The engine is looking for the two
firing *together*, so the 13F side and the insider side are scored separately
first (`score_13f`, `insider_score`) and a capped bonus is added when both clear
their thresholds, scaled by whichever side is weaker. Every stock also carries a
deterministic `signal_class`, the strongest being
`EARLY_SMART_MONEY_WITH_INSIDER_CONFIRMATION`.

A capped **price-action penalty** (max −15) is subtracted for names that already
ran ≥15 % / ≥25 % since the quarter-end. Ties break on insider score, then
consensus, then ticker A→Z. CUSIP-only rows (no resolvable ticker) are scored
but never enter the Top 10 because they cannot be traded or looked up.

Grades: `VERY_STRONG ≥ 75`, `STRONG ≥ 60`, `MODERATE ≥ 45`, `WEAK < 45`.

Every run stores an **input fingerprint** and a **ranking fingerprint**
(SHA-256) in `data/holdings/<date>_signals.json`; identical inputs produce
identical fingerprints. `tests/test_signal_engine.py` asserts this, including
order-independence of the input.

### Why each Top-10 stock qualifies

`signal_engine.py` produces a rule-based `why` block per stock – the three
largest point contributors, one reason sentence per factor, the insider
read, and any price-action caveat. `explain_signals.py` adds a short LLM
narrative on top; if no model output is available the rule-based text is used
verbatim, so the report never depends on an API call.

---

### Matching a filer to its own prior quarter

The prior quarter is joined on **CIK, not on the display name**. The names in
`config.FILERS` are ours and they change: relabelling "TCI Fund (Chris Hohn)" to
"Scion Asset Management (Burry)" would make every carried-over holding look like
a brand-new position. The reverse is worse - "Coatue (Laffont)" kept its label
while its CIK was corrected away from Chewy's, and a name-keyed join would diff
Chewy's book against Coatue's and invent a full set of EXITs and NEW positions.
A changed CIK under an unchanged name means a different entity, so that filer is
treated as having no prior quarter.

---

## Insider activity (SEC Form 4)

`insider_activity.py` resolves each candidate ticker to its issuer CIK
(SEC `company_tickers.json`), lists Form 4 / 4-A filings on EDGAR since the
last 13F quarter-end, downloads the raw XML and aggregates the non-derivative
table:

* code **P** + acquired → open-market purchase
* code **S** + disposed → open-market sale
* grants, exercises, gifts, tax withholding and derivative rows are ignored

Sales are split by intent. A Rule 10b5-1 sale was scheduled months in advance
under a pre-arranged plan and says nothing about how an insider sees the
business today, so only **discretionary** selling subtracts from the score; the
planned portion is reported but not penalised (SEC Form 4 flag `<aff10b5One>`).
On the September 2026 run this moved Carvana from rank 8 to rank 3: \$25.4M of
its \$27.6M in sales were pre-arranged, leaving \$2.2M discretionary.

Buyers are weighted by seniority (CEO/CFO > other officers > directors > 10 %
owners), independent buyers inside a 30-day window count as **cluster buying**,
and a purchase is measured against the buyer's own existing stake.

Two filters keep the numbers about the right company and the right security:

* **Issuer verification.** A company's EDGAR feed also carries Form 4s that the
  company itself filed as an insider (10 % owner) of a *different* issuer. The
  `<issuer>` CIK in each filing must match the ticker being scored. Without this,
  Uber's sale of Aurora Innovation shares was counted as $471.6 M of insider
  selling in UBER, and Berkshire's sale of DaVita shares as $36.5 M of selling
  in BRK/B.
* **Common stock only.** The non-derivative table can also report preferred
  stock, warrants, units and notes. Bank of America's "Preferred Stock,
  Series DD" was scored as a common-stock insider buy before this filter.

The summary carries a `net_stance` (`NET_BUYING` / `NET_SELLING` / `BALANCED` /
`NO_ACTIVITY`), and the report headline states that stance rather than the gross
purchase total, so a net seller is never labelled as confirming the 13F signal.

Only the top `SIGNAL_CANDIDATE_POOL` (40) pre-ranked tickers are looked up to
bound EDGAR traffic; results are cached per (ticker, window) under
`data/insider_cache/` and the score is re-derived from the cached raw summary
on every run.

---

## Call option selection

Predefined filters in `config.py` – a contract must pass **all** of them:

| Filter | Default |
|---|---|
| Days to expiration | 90–180 |
| Call delta | 0.30–0.70 (target 0.45) |
| Bid-ask spread | ≤ 8 % of mid |
| Volume | ≥ 300 (waived when open interest ≥ 2,500) |
| Open interest | ≥ 500 (never waived) |
| Implied volatility | ≤ 70 % |

Daily volume is a counter that resets every morning, and the scheduled run
fires 30 minutes after the US open, so a chain pulled then under-reports volume
for every strike. Open interest is session-independent, so a contract carrying
deep open interest clears the volume floor on its own; open interest itself is
never waived. The waiver is a function of the data, not of wall-clock time, so
the selection stays deterministic.

Among eligible contracts the pick minimises a deterministic cost (distance to
target delta, spread, distance from the middle of the expiry window, minus a
liquidity credit); ties break on symbol. Nothing eligible →
`NO_SUITABLE_OPTION_FOUND`, with the per-filter rejection counts shown in the
report. There is no relaxed fallback.

---

## Cost-aware model routing and cascading

`llm_router.py` is the only place the Claude API is called.

* **Routing** – `config.LLM_TASK_ROUTES` maps each task to an ordered list of
  tiers: `claude-haiku-4-5` → `claude-sonnet-5` → `claude-opus-5`.
* **Cascading** – the cheapest tier runs first; its structured output is
  validated (ticker set, lengths, enum values, no advice language, and no claim
  that contradicts the Form 4 totals - every dollar figure in the narrative must
  match a computed total, and text may not deny sales that happened). Only on a
  validation failure or API error does the router escalate one tier. Opus 5 is
  the quality anchor at the top of the cascade.
* **One batched call** – all ten stocks (context, thesis, insider read, risks,
  option note) are produced in a single tool-use call; the shared system
  prompt carries a `cache_control` breakpoint.
* **Content-hash cache** – requests are keyed by SHA-256 of task + prompts +
  schema in `data/llm_cache/`; re-running on identical data costs nothing and
  returns the identical narrative.
* **Budget guard** – `LLM_MAX_RUN_COST_USD` (default $1.50) per run; when it
  is exhausted, or no credentials exist, the deterministic rule-based
  explanation ships instead.
* **Ledger** – every call's tokens and estimated cost are appended to
  `data/llm_cache/usage_ledger.jsonl`; the report footer shows the route
  taken, cache hits and estimated cost.

Typical run: one Haiku call, well under $0.05.

---

## Setup

### GitHub Secrets

| Secret | Purpose |
|---|---|
| `ANTHROPIC_API_KEY` | commentary (optional – rule-based fallback otherwise) |
| `TRADIER_API_KEY` | option chains (optional – stocks show `NOT_EVALUATED`) |
| `GMAIL_ADDRESS` / `GMAIL_APP_PASSWORD` | sending the report |
| `REPORT_RECIPIENT` | optional; defaults to `GMAIL_ADDRESS` |
| `OPENFIGI_API_KEY` | optional; higher CUSIP-mapping limits |

Gmail App Password: enable 2-factor auth → *App passwords* → Mail → copy the
16-character password.

### Local

```bash
pip install -r requirements.txt
export ANTHROPIC_API_KEY=... TRADIER_API_KEY=... GMAIL_ADDRESS=... GMAIL_APP_PASSWORD=...
python src/run_pipeline.py --no-email          # full run, writes reports/<date>_report.html
python src/run_pipeline.py --from signals      # re-rank from existing scores
python -m pytest -q tests                      # determinism, option filters, Form 4 parsing, router cascade
```

`SEC_DATA_DIR`, `SEC_REPORTS_DIR` and `SEC_RUN_DATE` override the data
folders and the run date for dry runs.

---

## Data layout

```
data/holdings/<date>_raw_holdings.json      raw 13F XML + CUSIP map
data/holdings/<date>_holdings_parsed.json   deltas, weights, tiers
data/holdings/<date>_scores.json            alpha components, crowding, sell signals
data/holdings/<date>_signals.json           ★ SIGNAL SCORE ranking + Top 10 + fingerprints
data/holdings/<date>_options.json           selected Call / NO_SUITABLE_OPTION_FOUND per stock
data/holdings/<date>_final_analysis.json    Top 10 + commentary + option + LLM route
data/insider_cache/                         Form 4 look-ups per ticker/window (git-ignored: raw EDGAR cache)
data/llm_cache/                             content-hash LLM cache + usage ledger
reports/<date>_report.html                  the e-mailed report
```

---

## Monitored institutions

52 filers, all CIKs verified against `https://data.sec.gov/submissions/CIK{cik}.json`
(see `config.FILERS`). NVIDIA Corp and Alphabet Inc file 13F for strategic
corporate stakes rather than a stock-picking mandate; their static quality prior
is set lower.

**CIK corrections** (a wrong CIK silently yields a wrong or empty portfolio, so
each was re-verified against the EDGAR submissions feed):

| Entry | Was | Now | Why |
|---|---|---|---|
| Coatue (Laffont) | `0001766502` | `0001135730` | the old CIK is **Chewy, Inc.**, not Coatue Management LLC |
| Duke University (DUMAC) | `0001439873` | `0001584258` | the old CIK files only SC 13G; the endowment's 13F filer is DUMAC, Inc. |
| Scion Asset Management (Burry) | labeled "TCI Fund (Chris Hohn)" | `0001649339` | mislabeled entry; TCI Fund Management Ltd has its own row |

---

## Data-quality gate (fails closed)

`data_quality.py` sits between parsing and scoring. A quarter-over-quarter
comparison can break silently in ways that still look like a successful run - a
missing prior quarter, a prior file for the *same* quarter, a filer join that
matches nothing, share counts that never parsed - and every one of them produces
the same symptom: everything reads NEW, nothing is REDUCED, nothing EXITs.

The gate stops the run rather than ranking that noise:

```
DELTA SUMMARY
  Current reporting quarter: 2026-06-30
  Expected prior quarter:    2026-03-31
  Loaded prior period:       2026-03-31
  NEW / ADDED / REDUCED / UNCHANGED / EXIT ...
  Matched current/prior positions: ...
DATA QUALITY GATE: PASS
```

It fails when the loaded prior period is not the required one, when no position
matched the prior quarter, or when more than 95 % of positions read as NEW. On
failure nothing downstream runs: no scoring, no signal engine, no Form 4
look-up, no options, no report, no e-mail. A green GitHub Actions run therefore
cannot mean "invalid signals were generated successfully".

---

## Data-quality guards

Every one of these was a silent failure found in a live run, so each now has a
guard or a loud warning:

| Guard | What it prevents |
|---|---|
| Share counts read from the nested `<shrsOrPrnAmt><sshPrnamt>` | A direct-child lookup returned nothing, so **every position ever parsed had 0 shares**: everything read as NEW, no EXIT was ever detected and the share-count delta the ranking is built on never worked |
| Only filings whose `reportDate` equals the target quarter are used | Yale and Scion contributed Q3-2025 books and Pershing Square a Q1-2026 book to a Q2-2026 run; a filer without the current quarter is reported as `STALE_FILER` and excluded |
| Prior quarter selected by reporting quarter, not file date | Re-running the pipeline wrote a second file for the same quarter, and the next run diffed that quarter against itself |
| Portfolio weights divide by the full reported book | Quant books are stored capped at 500 positions; dividing by the capped sum inflated every weight, and EXITs are not derived for capped filers because a rank drop is indistinguishable from a sale |
| Filer history keyed on CIK, in both the delta join and the manager-quality chain | A rename split one manager into two half-length histories and left the wrong old name standing |
| Unmapped CUSIPs never reach the price feed, the candidate pool or the Top 10 (`tradable_ticker_validated`) | Roughly a hundred failed downloads per run, and CUSIP strings appearing in the ranking. They stay in the book for AUM and weight accounting |
| The information table is checked against the filing's own cover page when its filename suggests another period | SurgoCap ships a Q2-2026 filing whose table is named `Surgo_13F_09302025.xml`; the cover page confirms the real period, so the filename alone is never trusted |
| Multi-quarter history is deduplicated by reporting quarter | Two pipeline runs for one quarter would otherwise count as two quarters of accumulation |
| The latest filing for the quarter wins, amendments preferred on a tie | A 13F-HR/A restates the original |
| Zero EXITs or all-NEW across the universe raises a data-quality warning | The comparison being broken is far more likely than a quarter in which nobody sold anything |

---

## Track record

Every report ends with the running 90-day record **against the S&P 500 (SPY)**:
average excess return, share of signals that beat the benchmark, and a split
between the current deterministic engine and the earlier LLM-selected top-5
pipeline. Legacy rows are reported separately because they are not evidence for
the current engine.

---

## Known limitations

| Limitation | Handling |
|---|---|
| 13F data is up to 45 days old and shows no shorts, hedges or cash | freshness factor, price-action penalty, disclaimer |
| Weights use long-only reported AUM | overstated for diversified managers – noted in the report |
| Only the 60 top pre-ranked tickers get the Form 4 look-up | a name outside that pool cannot enter the Top 10 on insider strength alone; raised from 40 because insider data can now move a name by up to 25 points |
| Share classes of one issuer share a Top-10 slot | the higher-scoring class is kept; the other is listed as an alternate. Folding requires the issuer name **and** the ticker family to match, because every iShares fund reports the issuer name "ISHARES INC" |
| A filer absent from the prior quarter makes all its positions look NEW | expected once after the universe is expanded; self-corrects next quarter |
| Crowding is a proxy (hotel list + tracked-fund cluster) | no market-wide ownership feed is wired up |
| Manager quality blends toward a static prior until enough quarters exist | fully data-driven after 8 quarters |
| Option quotes are delayed snapshots | verify before trading |

This tool is for educational and informational purposes only. Nothing here is
investment advice. Options can expire worthless.
