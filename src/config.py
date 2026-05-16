"""
config.py
Central configuration for SEC 13F Smart Money Analyzer.
All CIKs, thresholds, and API endpoints defined here.
"""

from pathlib import Path

# ── Directory layout ──────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).parent.parent
DATA_DIR = BASE_DIR / "data" / "holdings"
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
OPENFIGI_BATCH   = 100   # max items per request

# ── Scoring weights ──────────────────────────────────────────────────────────
# Reduced portfolio weight dominance so a single fund with high concentration
# doesn't automatically win. Cluster signal now much stronger.
WEIGHT_PORTFOLIO_PCT  = 0.20   # how large the position is in the portfolio
WEIGHT_DELTA_PCT      = 0.20   # how aggressively the manager bought
# Cluster bonus (CLUSTER_BONUS_MULTIPLIER) provides the remaining signal weight

# ── Scoring thresholds ────────────────────────────────────────────────────────
MIN_PORTFOLIO_WEIGHT_PCT   = 0.5    # ignore positions < 0.5% of portfolio
HIGH_CONVICTION_TOP_N      = 10     # "top 10" threshold for new position flag
HIGH_CONVICTION_MIN_PCT    = 3.0    # new position >= 3% = HIGH CONVICTION flag
CLUSTER_MIN_FUNDS          = 2      # 2+ funds buying = cluster signal
CLUSTER_BONUS_MULTIPLIER   = 3.0    # strong bonus: 6-fund cluster >> 1-fund
DOUBLE_DOWN_MIN_DELTA      = 20.0   # +20% shares while price fell = double-down

# ── Tradier API ───────────────────────────────────────────────────────────────
TRADIER_BASE_URL    = "https://api.tradier.com/v1"   # Live account
# TRADIER_BASE_URL  = "https://sandbox.tradier.com/v1"  # Paper account
OPTION_MIN_VOLUME   = 100
OPTION_DELTA_MIN    = 0.30
OPTION_DELTA_MAX    = 0.70
OPTION_MIN_DAYS     = 90
OPTION_MAX_DAYS     = 180

# ── Claude API ────────────────────────────────────────────────────────────────
CLAUDE_MODEL        = "claude-sonnet-4-6"
CLAUDE_MAX_TOKENS   = 4096
CLAUDE_RETRY_COUNT  = 3
CLAUDE_RETRY_DELAY  = 5   # seconds

# ── Gmail ─────────────────────────────────────────────────────────────────────
GMAIL_SMTP_HOST = "smtp.gmail.com"
GMAIL_SMTP_PORT = 587
REPORT_SUBJECT  = "📊 SEC 13F Smart Money Report – {date}"
