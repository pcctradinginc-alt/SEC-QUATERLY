"""
config.py
Central configuration for SEC 13F Smart Money Analyzer.
All CIKs, thresholds, and API endpoints defined here.
"""

from pathlib import Path

# ── Directory layout ──────────────────────────────────────────────────────────
BASE_DIR    = Path(__file__).parent.parent
DATA_DIR    = BASE_DIR / "data" / "holdings"
REPORTS_DIR = BASE_DIR / "reports"
DATA_DIR.mkdir(parents=True, exist_ok=True)
REPORTS_DIR.mkdir(parents=True, exist_ok=True)

# ── Filer registry ────────────────────────────────────────────────────────────
# Format: { "display_name": "CIK_padded_to_10_digits" }
FILERS = {
    "Situational Awareness LP":   "0002045724",
    "Yale University":            "0000938582",
    "Gates Foundation Trust":     "0001166559",
    "Harvard Management Co":      "0001082621",
    "Brown University":           "0001664741",
    "Duke University":            "0001439873",
    "TCI Fund (Chris Hohn)":      "0001649339",
    "Pershing Square (Ackman)":   "0001336528",
    "Tiger Global (Coleman)":     "0001167483",
    "Coatue (Laffont)":          "0001766502",
    "D1 Capital (Sundheim)":      "0001747057",
    "Viking Global (Halvorsen)":  "0001103804",
    "AQR Capital (Asness)":       "0001167557",
}

# ── Filer quality tiers ───────────────────────────────────────────────────────
# University endowments: long-horizon, fundamental → higher weight
# Growth hedge funds: more momentum-driven → lower weight
# These multipliers are applied to raw_score in scoring.py
FILER_QUALITY: dict[str, float] = {
    "Yale University":            1.3,
    "Harvard Management Co":      1.3,
    "Gates Foundation Trust":     1.3,
    "Brown University":           1.2,
    "Duke University":            1.2,
    "TCI Fund (Chris Hohn)":      1.2,
    "Viking Global (Halvorsen)":  1.1,
    "AQR Capital (Asness)":       1.0,
    "Pershing Square (Ackman)":   1.0,
    "Tiger Global (Coleman)":     0.9,
    "Coatue (Laffont)":          0.9,
    "D1 Capital (Sundheim)":      0.9,
    "Situational Awareness LP":   0.8,
}

# ── SEC EDGAR endpoints ───────────────────────────────────────────────────────
EDGAR_SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"
EDGAR_ARCHIVES_URL    = "https://www.sec.gov/Archives/edgar/data/{cik_int}/{accession_dashes}/{filename}"
SEC_HEADERS = {
    "User-Agent": "SmartMoneyAnalyzer research@example.com",  # SEC requires this
    "Accept-Encoding": "gzip, deflate",
}
SEC_RATE_LIMIT_SLEEP = 0.15   # seconds between requests (max 10/s allowed)

# ── OpenFIGI (CUSIP → Ticker mapping) ────────────────────────────────────────
OPENFIGI_URL     = "https://api.openfigi.com/v3/mapping"
OPENFIGI_BATCH   = 100   # up to 100 with API key (openfigi.com), 10 without
OPENFIGI_API_KEY = ""    # optional – set as GitHub Secret OPENFIGI_API_KEY for higher limits

# ── Scoring weights ───────────────────────────────────────────────────────────
WEIGHT_PORTFOLIO_PCT = 0.20   # how large the position is in the portfolio
WEIGHT_DELTA_PCT     = 0.20   # how aggressively the manager bought

# ── Scoring thresholds ────────────────────────────────────────────────────────
MIN_PORTFOLIO_WEIGHT_PCT  = 0.5    # ignore positions < 0.5% of portfolio
HIGH_CONVICTION_MIN_PCT   = 3.0    # new position >= 3% = HIGH CONVICTION flag
CLUSTER_MIN_FUNDS         = 2      # 2+ funds buying = cluster signal
DOUBLE_DOWN_MIN_DELTA     = 20.0   # +20% shares while price fell = double-down

# ── Price-action staleness check ─────────────────────────────────────────────
# Compares current price to price at filing date (via yfinance).
# Prevents recommending stocks that have already fully played out.
PRICE_ACTION_WARN_PCT      = 15.0  # add WARNING flag if up >15% since filing
PRICE_ACTION_DOWNGRADE_PCT = 25.0  # halve the score if up >25% since filing

# ── Multi-quarter conviction tracking ────────────────────────────────────────
MULTI_QUARTER_MAX       = 8    # look back up to 8 quarters of history
MULTI_QUARTER_BUILD_MIN = 3    # 3+ consecutive build quarters = strong signal
MULTI_QUARTER_BONUS     = 1.5  # score multiplier for confirmed multi-quarter builds

# ── Manager Quality (dynamic, 0-1) ───────────────────────────────────────────
# Replaces the old static FILER_QUALITY lookup with a data-driven estimate.
# FILER_QUALITY above is kept and used as a bootstrap prior until enough
# quarters of history exist (see manager_quality.py).
MANAGER_QUALITY_MIN_HISTORY_QUARTERS = 2      # below this: mostly prior, some concentration
MQ_CONCENTRATION_FLOOR_POSITIONS     = 10     # <=10 positions -> concentration score 1.0
MQ_CONCENTRATION_CEIL_POSITIONS      = 200    # >=200 positions -> concentration score 0.0
MQ_TURNOVER_FULL_PENALTY_PCT         = 60.0   # >=60% quarterly turnover -> turnover score 0.0

# ── Freshness (Filing Delay x Turnover decay) ────────────────────────────────
# Freshness = exp(-FRESHNESS_K * turnover_fraction * filing_delay_fraction)
# filing_delay_fraction = (filingDate - reportDate) in days, normalized to a quarter.
FRESHNESS_K             = 2.0
FRESHNESS_QUARTER_DAYS  = 90.0

# ── New-position size bands (Section 3) ──────────────────────────────────────
# (lower_bound_pct_inclusive, label), ascending – the highest matching lower
# bound wins. <1.0% falls through to "WEAK".
NEW_POSITION_BANDS = [
    (0.0,  "WEAK"),
    (1.0,  "INTERESTING"),
    (3.0,  "STRONG"),
    (5.0,  "VERY_STRONG"),
    (10.0, "EXCEPTIONAL"),
]
# A position >= this many times the filer's own median position weight is
# flagged as an outsized bet for that manager, regardless of absolute %
# (Section 3: "5% ist bei einem Fonds mit 10 Aktien etwas anderes als bei 200").
RELATIVE_OUTSIZED_VS_MEDIAN = 5.0

# ── Price-action penalty points (additive, replaces the old score-halving) ──
PRICE_ACTION_STALE_PENALTY = 30.0   # points subtracted if > PRICE_ACTION_DOWNGRADE_PCT
PRICE_ACTION_WARN_PENALTY  = 10.0   # points subtracted if > PRICE_ACTION_WARN_PCT

# ── Early Smart Money Accumulation (Section 10) ──────────────────────────────
EARLY_SMART_MONEY_MIN_FUNDS          = 2
EARLY_SMART_MONEY_MAX_FUNDS          = 6
EARLY_SMART_MONEY_MIN_AVG_QUALITY    = 0.6
EARLY_SMART_MONEY_MAX_BUILD_QUARTERS = 3   # "seit wenigen Quartalen sichtbar"

# ── Crowding penalty (proxy – Tier 2) ────────────────────────────────────────
# NOTE: This is NOT real market-wide institutional ownership data (that is
# Tier 3, section 11-13, and needs a data source beyond the 13 tracked filers).
# It approximates crowding from (a) how many of the 13 tracked funds already
# hold the name, and (b) a static list of well-known "hedge fund hotel" mega
# caps that are structurally crowded regardless of what our 13 funds do.
CROWDING_HOTEL_TICKERS = {
    "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "GOOG", "META", "TSLA",
    "BRK/B", "BRK.B", "AVGO", "LLY",
}
CROWDING_HOTEL_PENALTY               = 25.0  # flat points if ticker is in the proxy hotel list
CROWDING_PENALTY_PER_FUND_OVER_MAX   = 8.0   # points per fund beyond EARLY_SMART_MONEY_MAX_FUNDS
CROWDING_LABEL_BANDS = [   # (max_score_inclusive, label)
    (25.0, "LOW"),
    (50.0, "MODERATE"),
    (75.0, "HIGH"),
    (float("inf"), "EXTREME"),
]

# ── 13F Alpha Score weights (Section 15) ─────────────────────────────────────
# "active_weight" is proxied by portfolio-weight percentile since no benchmark
# index data is wired up yet (Tier 3 gap – Section 5).
# "abnormal_ownership" has no data source yet (Tier 3 gap – Section 11-12) and
# always contributes 0; kept as an explicit line item so the gap stays visible
# in the score breakdown rather than being silently redistributed.
ALPHA_WEIGHTS = {
    "active_weight":      0.25,
    "position_change":    0.20,
    "manager_quality":    0.15,
    "consensus":          0.15,
    "accumulation":       0.10,
    "freshness":          0.10,
    "abnormal_ownership": 0.05,
}

# ── Tradier API ───────────────────────────────────────────────────────────────
TRADIER_BASE_URL    = "https://api.tradier.com/v1"   # Live account
# TRADIER_BASE_URL  = "https://sandbox.tradier.com/v1"  # Paper account
OPTION_MIN_VOLUME   = 300    # was 100 – stricter liquidity requirement
OPTION_MAX_SPREAD_PCT = 8.0  # skip options with bid-ask spread > 8% of mid
OPTION_DELTA_MIN    = 0.30
OPTION_DELTA_MAX    = 0.70
OPTION_MIN_DAYS     = 90
OPTION_MAX_DAYS     = 180
OPTION_MAX_IV       = 0.70   # skip options with IV > 70% (overpriced premium)

# ── Claude API ────────────────────────────────────────────────────────────────
# Round 1 (top-20 screening): Haiku is sufficient and ~20× cheaper than Sonnet
# Round 2 (precise option selection): Sonnet for nuanced financial reasoning
CLAUDE_MODEL_R1   = "claude-haiku-4-5-20251001"
CLAUDE_MODEL_R2   = "claude-sonnet-4-6"
CLAUDE_MODEL      = CLAUDE_MODEL_R2   # backward-compat alias
CLAUDE_MAX_TOKENS = 4096
# Round 1 now returns the full Section-20 structured format (manager activity
# table + 5 narrative fields + bullet lists per stock, x5 stocks) - needs more room.
CLAUDE_MAX_TOKENS_R1 = 8192
CLAUDE_RETRY_COUNT = 3
CLAUDE_RETRY_DELAY = 5   # seconds

# ── Gmail ─────────────────────────────────────────────────────────────────────
GMAIL_SMTP_HOST = "smtp.gmail.com"
GMAIL_SMTP_PORT = 587
REPORT_SUBJECT  = "📊 SEC 13F Smart Money Report – {date}"
