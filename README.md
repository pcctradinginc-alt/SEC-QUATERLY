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
| Insider buying (Form 4) | 15 | open-market purchases since the 13F quarter-end: value, distinct insiders, officer/director involvement; net sellers capped |
| Filing freshness | 10 | `exp(-k · turnover · filing delay)` and the age of the filing |
| Institutional crowding | 5 | inverse of the crowding proxy (hedge-fund-hotel list + oversized cluster) |

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

## Insider activity (SEC Form 4)

`insider_activity.py` resolves each candidate ticker to its issuer CIK
(SEC `company_tickers.json`), lists Form 4 / 4-A filings on EDGAR since the
last 13F quarter-end, downloads the raw XML and aggregates the non-derivative
table:

* code **P** + acquired → open-market purchase
* code **S** + disposed → open-market sale
* grants, exercises, gifts and derivative rows are ignored

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
  validated (ticker set, lengths, enum values, no advice language). Only on a
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
data/insider_cache/                         Form 4 look-ups per ticker/window
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

## Known limitations

| Limitation | Handling |
|---|---|
| 13F data is up to 45 days old and shows no shorts, hedges or cash | freshness factor, price-action penalty, disclaimer |
| Weights use long-only reported AUM | overstated for diversified managers – noted in the report |
| Only the 40 top pre-ranked tickers get the Form 4 look-up | a name outside that pool cannot enter the Top 10 on insider strength alone |
| Share classes of one issuer share a Top-10 slot | the higher-scoring class is kept; the other is listed as an alternate |
| A filer absent from the prior quarter makes all its positions look NEW | expected once after the universe is expanded; self-corrects next quarter |
| Crowding is a proxy (hotel list + tracked-fund cluster) | no market-wide ownership feed is wired up |
| Manager quality blends toward a static prior until enough quarters exist | fully data-driven after 8 quarters |
| Option quotes are delayed snapshots | verify before trading |

This tool is for educational and informational purposes only. Nothing here is
investment advice. Options can expire worthless.
