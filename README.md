# SEC 13F Smart Money Analyzer

Automated quarterly analysis of 13F filings from 13 top institutional investors.
Runs automatically on **16 May / 16 Aug / 16 Nov / 16 Feb** (next business day if weekend).
Delivers a **Top 5 stock picks + specific option trades** report via Gmail.

---

## Monitored Institutions

| Institution | CIK |
|---|---|
| Situational Awareness LP | 0002045724 |
| Yale University | 0000938582 |
| Gates Foundation Trust | 0001166559 |
| Harvard Management Co | 0001082621 |
| Brown University | 0001664741 |
| Duke University | 0001439873 |
| TCI Fund (Chris Hohn) | 0001649339 |
| Pershing Square (Ackman) | 0001336528 |
| Tiger Global (Coleman) | 0001167483 |
| Coatue (Laffont) | 0001766502 |
| D1 Capital (Sundheim) | 0001747057 |
| Viking Global (Halvorsen) | 0001103804 |
| AQR Capital (Asness) | 0001167557 |

---

## Pipeline

```
GitHub Actions Trigger (14:00 UTC, 14th-20th of target months)
    │
    ├─ date_check.py        → Is today the right run date?
    ├─ fetch_filings.py     → SEC EDGAR: fetch 13F XMLs for all 13 CIKs
    │                          + CUSIP→Ticker mapping (OpenFIGI)
    │                          + Stock split detection (yfinance)
    ├─ parse_13f.py         → Delta vs. prior quarter (incl. EXITs), portfolio weights,
    │                          rank tiers, corporate-action heuristic
    ├─ manager_quality.py   → Dynamic Manager Quality score (concentration + turnover)
    ├─ scoring.py           → 13F Alpha Score, clustering, crowding proxy,
    │                          Early Smart Money detection, sell-side signals
    ├─ analyze_claude_round1.py → Claude: Top 5 stocks + investment theses
    ├─ options_lookup.py    → Tradier: Real option chains for Top 5
    ├─ analyze_claude_round2.py → Claude: Select best specific option per stock
    └─ send_report.py       → HTML report → Gmail
```

---

## Setup

### 1. Fork / Clone this repo

```bash
git clone https://github.com/YOUR_USERNAME/sec-smart-money.git
cd sec-smart-money
```

### 2. Add GitHub Secrets

Go to **Settings → Secrets and variables → Actions → New repository secret**:

| Secret Name | Value |
|---|---|
| `ANTHROPIC_API_KEY` | Your Anthropic API key |
| `TRADIER_API_KEY` | Your Tradier API key (live or sandbox) |
| `GMAIL_ADDRESS` | Your Gmail address (e.g. `you@gmail.com`) |
| `GMAIL_APP_PASSWORD` | Gmail App Password (not your login password) |

**How to create a Gmail App Password:**
1. Go to [myaccount.google.com/security](https://myaccount.google.com/security)
2. Enable 2-Factor Authentication if not already
3. Search for "App passwords"
4. Create a new app password → select "Mail" and "Other (custom)"
5. Copy the 16-character password into the `GMAIL_APP_PASSWORD` secret

**Tradier API:**
- Live account: use `https://api.tradier.com/v1` (default in config.py)
- Paper account: change `TRADIER_BASE_URL` in `src/config.py` to `https://sandbox.tradier.com/v1`

### 3. Configure Tradier Account Type

Edit `src/config.py`:
```python
TRADIER_BASE_URL = "https://api.tradier.com/v1"   # Live (default)
# TRADIER_BASE_URL = "https://sandbox.tradier.com/v1"  # Paper
```

### 4. Test a Manual Run

Go to **Actions → SEC 13F Quarterly Analysis → Run workflow**
Set `force_run = true` to bypass the date check.

---

## Local Development

```bash
pip install -r requirements.txt

# Set environment variables
export ANTHROPIC_API_KEY="your_key"
export TRADIER_API_KEY="your_key"
export GMAIL_ADDRESS="you@gmail.com"
export GMAIL_APP_PASSWORD="your_app_password"

# Run individual steps
python src/fetch_filings.py
python src/parse_13f.py
python src/scoring.py
python src/analyze_claude_round1.py
python src/options_lookup.py
python src/analyze_claude_round2.py
python src/send_report.py
```

---

## Data Architecture

```
data/holdings/
  YYYY-MM-DD_raw_holdings.json      ← Raw SEC XML data + CUSIP mapping
  YYYY-MM-DD_holdings_parsed.json   ← Delta-enriched positions
  YYYY-MM-DD_scores.json            ← Conviction scores
  YYYY-MM-DD_claude_round1.json     ← Top 5 stocks from Claude
  YYYY-MM-DD_options.json           ← Tradier options data
  YYYY-MM-DD_final_analysis.json    ← Combined analysis
reports/
  YYYY-MM-DD_report.html            ← Final HTML report (committed to repo)
```

---

## Known Limitations (by design)

| Limitation | Impact | Mitigation |
|---|---|---|
| 13F data is 45 days old | Positions may have changed | Use as idea generator only |
| Long-only AUM denominator | Portfolio weights overstated | Disclaimer in report |
| No shorts/hedges visible | Incomplete picture | Noted in report |
| Stock splits adjusted | High accuracy, not perfect | yfinance split check |
| Options prices at analysis time | Change constantly | Always verify before trading |
| No benchmark index data | "Active Weight" vs. S&P 500/Russell not available | Proxied by weight vs. the manager's own median position |
| No market-wide ownership data | Only the 13 tracked filers are observed, not all 13F filers | Report says so explicitly instead of guessing a trend |
| No Abnormal Ownership model | Needs market cap/sector/float data not wired up | Component always reports 0, kept visible in the score breakdown |
| Manager Quality is not backtest-calibrated yet | Only 2-3 quarters of history exist so far | Blends toward a static prior until more quarters accumulate |
| Merger/spin-off detection is heuristic | Name+value matching can miss or misfire | Flagged `possible_corporate_action`, excluded from scoring either way |

---

## 13F Alpha Score

Additive, component-based score (0-100) per ticker, computed in [`scoring.py`](src/scoring.py):

```
13F Alpha Score =
    25% × Active Weight        (proxied by portfolio-weight percentile — no benchmark data yet)
  + 20% × Position Change      (share-count delta magnitude, percentile)
  + 15% × Manager Quality      (dynamic 0-1 score — see manager_quality.py)
  + 15% × Smart-Money Consensus(quality-weighted multi-fund buying)
  + 10% × Multi-Quarter Accumulation (recency-weighted: 0.5×current + 0.3×prior + 0.2×Q-2)
  + 10% × Freshness            (exp(-k × turnover × filing_delay) — see manager_quality.py)
  +  5% × Abnormal Ownership   (no data source yet — always 0, kept visible)
  −  Crowding Penalty          (proxy: hedge-fund-hotel list + oversized same-run cluster)
  −  Price-action penalty      (stock already ran hard since the filing date)
```

Manager Quality replaces a hand-assigned lookup table with concentration (position
count) and turnover (value-weighted portfolio churn quarter over quarter), blended
toward a static prior until enough quarters of history exist to trust the data alone.

**Flags:**
- `HIGH_CONVICTION`: New position ≥3% of portfolio
- `NEW_POSITION`: Not in prior quarter's filing
- `SIZE_WEAK` / `SIZE_INTERESTING` / `SIZE_STRONG` / `SIZE_VERY_STRONG` / `SIZE_EXCEPTIONAL`: new-position size band
- `OUTSIZED_VS_MANAGER_TYPICAL`: position ≥5× that manager's own median position size
- `TOP3_POSITION` / `TOP5_POSITION` / `TOP10_ENTRY`: rank within the filer's own portfolio
- `AGGRESSIVE_ADD`: Position increased >20% by share count
- `CLUSTER`: 2+ monitored funds buying same ticker simultaneously
- `EARLY_SMART_MONEY_ACCUMULATION`: 2-6 high-quality managers building the same name early, still low/moderate crowding — the single most-preferred setup
- `PRICE_ACTION_STALE` / `PRICE_ACTION_WARNING`: stock has already run since the filing date

**Sell-side signals** (REDUCE ≥20% cut, or a full EXIT) are scored separately in
`build_sell_signals()` and shown in the report as "Notable Exits & Reductions" —
they are informational risk context, not merged into the buy-side Top 5.

---

## Disclaimer

This tool is for educational and informational purposes only. 13F data is publicly
available but delayed. Nothing in this repository constitutes investment advice.
Options trading involves significant risk of loss. Always conduct your own research
before making any investment decisions.
