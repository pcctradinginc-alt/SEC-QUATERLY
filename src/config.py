"""
config.py
Central configuration for SEC 13F Smart Money Analyzer.
All CIKs, thresholds, and API endpoints defined here.
"""

from pathlib import Path

# ── Directory layout ──────────────────────────────────────────────────────────
import os
BASE_DIR    = Path(__file__).parent.parent
# SEC_DATA_DIR / SEC_REPORTS_DIR let tests and local dry-runs write elsewhere.
# `or` (not a get() default): an env var defined but empty must fall back too,
# otherwise Path("") silently becomes the current directory.
DATA_DIR    = Path(os.environ.get("SEC_DATA_DIR")    or BASE_DIR / "data" / "holdings")
REPORTS_DIR = Path(os.environ.get("SEC_REPORTS_DIR") or BASE_DIR / "reports")


def run_date() -> str:
    """
    ISO date used to key every data file.

    SEC_RUN_DATE wins. Otherwise, when a specific quarter is being rebuilt
    (SEC_TARGET_REPORT_DATE), the run is dated shortly after that quarter's
    filing deadline so the baseline lands in its own file and sorts before the
    current run instead of overwriting it.
    """
    from datetime import date as _date, timedelta as _td
    explicit = os.environ.get("SEC_RUN_DATE", "").strip()
    if explicit:
        return explicit
    target = os.environ.get("SEC_TARGET_REPORT_DATE", "").strip()
    if target:
        try:
            return (_date.fromisoformat(target) + _td(days=46)).isoformat()
        except ValueError:
            pass
    return _date.today().isoformat()
DATA_DIR.mkdir(parents=True, exist_ok=True)
REPORTS_DIR.mkdir(parents=True, exist_ok=True)

# ── Filer registry ────────────────────────────────────────────────────────────
# Format: { "display_name": "CIK_padded_to_10_digits" }
# All CIKs verified against https://data.sec.gov/submissions/CIK{cik}.json
# (name match + confirmed 13F-HR filing history) before being added here.
#
# DATA-QUALITY FIX: the CIK previously labeled "TCI Fund (Chris Hohn)"
# (0001649339) actually resolves to Scion Asset Management, LLC (Michael
# Burry) on EDGAR - a mislabeled entry, not TCI Fund Management at all.
# Renamed below to the correct manager; the real TCI Fund Management Ltd
# CIK (0001647251) is added as its own entry. Any historical
# data/holdings/*.json from before this fix has Burry's Scion filings
# stored under the old "TCI Fund (Chris Hohn)" key.
FILERS = {
    "Situational Awareness LP":       "0002045724",
    "Yale University":                "0000938582",
    "Gates Foundation Trust":         "0001166559",
    "Harvard Management Co":          "0001082621",
    "Brown University":               "0001664741",
    "Duke University (DUMAC)":        "0001584258",  # was 0001439873 = Duke Univ. 13G-only filer
    "Scion Asset Management (Burry)": "0001649339",  # was mislabeled "TCI Fund (Chris Hohn)"
    "Pershing Square (Ackman)":       "0001336528",
    "Tiger Global (Coleman)":         "0001167483",
    "Coatue (Laffont)":               "0001135730",  # was 0001766502 = Chewy, Inc. (mislabeled)
    "D1 Capital (Sundheim)":          "0001747057",
    "Viking Global (Halvorsen)":      "0001103804",
    "AQR Capital (Asness)":           "0001167557",
    # --- Added: broader "smart money" universe ---
    "Berkshire Hathaway (Buffett)":         "0001067983",
    "Soros Fund Management (Soros)":        "0001029160",
    "Duquesne Family Office (Druckenmiller)":"0001536411",
    "NVIDIA Corp":                          "0001045810",
    "Alphabet Inc":                         "0001652044",
    "Third Point (Loeb)":                   "0001040273",
    "Baupost Group (Klarman)":              "0001061768",
    "TCI Fund Management (Hohn)":           "0001647251",
    "Pershing Square Inc":                  "0002026053",
    "SoftBank Group Corp":                  "0001065521",
    "Oaktree Capital Management (Marks)":   "0000949509",
    "Trian Fund Management (Peltz)":        "0001345471",
    "DME Capital Management":               "0001489933",
    "Renaissance Technologies (Quant)":     "0001037389",
    "Two Sigma Investments (Quant)":        "0001179392",
    "Thiel Macro (Thiel)":                  "0001562087",
    "Donald Smith & Co":                    "0000814375",
    "Whale Rock Capital Management":        "0001387322",
    "Appaloosa (Tepper)":                   "0001656456",
    "Chou Associates Management":           "0001389403",
    "7G Capital Management":                "0001720350",
    "Lountzis Asset Management":            "0001821168",
    "ValueAct Holdings":                    "0001418814",
    "H&H International Investment (Li Lu)": "0001759760",
    "Brave Warrior Advisors (Ainslie)":     "0001553733",
    "Arbiter Partners Capital Management":  "0001513193",
    "Sound Shore Management":               "0000820124",
    "Fairfax Financial Holdings (Watsa)":   "0000915191",
    "Semper Augustus Investments Group":    "0001115373",
    "Atreides Management":                  "0001777813",
    "RV Capital (Zeller)":                  "0001766596",
    "Ancient Art (Pabrai)":                 "0001426749",
    "Muhlenkamp & Co":                      "0001133219",
    "Himalaya Capital Management (Li Lu)":  "0001709323",
    "Abrams Capital Management":            "0001358706",
    "Lone Pine Capital (Mandel)":           "0001061165",
    "Dodge & Cox":                          "0000200217",
    "Harris Associates (Oakmark)":          "0000813917",
    "SurgoCap Partners":                    "0001960830",
}

# ── Economic decision-makers ──────────────────────────────────────────────────
# Several entries file separately but answer to the same investment process.
# Counting them as independent buyers manufactures consensus: PDD's "two
# independent managers" were both Li Lu vehicles. Consensus counts distinct
# groups; filers not listed here are their own group.
FILER_ECONOMIC_GROUP = {
    "Pershing Square (Ackman)":               "PERSHING_SQUARE",
    "Pershing Square Inc":                    "PERSHING_SQUARE",
    "H&H International Investment (Li Lu)":   "LI_LU",
    "Himalaya Capital Management (Li Lu)":    "LI_LU",
}

# Corporate treasuries file 13F for strategic stakes, not a stock-picking
# mandate. They stay in the data, but they cannot lend breadth to a consensus
# or count towards a cluster.
CORPORATE_STRATEGIC_FILERS = {"NVIDIA Corp", "Alphabet Inc", "SoftBank Group Corp"}


def economic_group(filer_name: str) -> str:
    return FILER_ECONOMIC_GROUP.get(filer_name, filer_name)


# ── Filer quality tiers (static bootstrap prior) ─────────────────────────────
# University endowments / concentrated value investors: long-horizon,
# fundamental -> higher weight. Quant / diversified / non-traditional
# filers (corporate treasuries, pure quant shops): lower weight.
# This is only the STARTING prior - manager_quality.py blends it with
# actual measured concentration + turnover once enough quarters of history
# exist, and the prior's influence shrinks as real data accumulates.
FILER_QUALITY: dict[str, float] = {
    "Yale University":            1.3,
    "Harvard Management Co":      1.3,
    "Gates Foundation Trust":     1.3,
    "Brown University":           1.2,
    "Duke University (DUMAC)":    1.2,
    "TCI Fund Management (Hohn)": 1.2,
    "Viking Global (Halvorsen)":  1.1,
    "AQR Capital (Asness)":       1.0,
    "Pershing Square (Ackman)":   1.0,
    "Pershing Square Inc":        1.0,
    "Tiger Global (Coleman)":     0.9,
    "Coatue (Laffont)":           0.9,
    "D1 Capital (Sundheim)":      0.9,
    "Situational Awareness LP":   0.8,
    # --- Added ---
    "Berkshire Hathaway (Buffett)":           1.3,
    "Baupost Group (Klarman)":                1.3,
    "ValueAct Holdings":                      1.3,
    "H&H International Investment (Li Lu)":   1.3,
    "Himalaya Capital Management (Li Lu)":    1.3,
    "Semper Augustus Investments Group":      1.2,
    "Fairfax Financial Holdings (Watsa)":     1.2,
    "RV Capital (Zeller)":                    1.2,
    "Oaktree Capital Management (Marks)":     1.2,
    "Harris Associates (Oakmark)":            1.2,
    "Dodge & Cox":                            1.2,
    "Ancient Art (Pabrai)":                   1.2,
    "Muhlenkamp & Co":                        1.1,
    "Donald Smith & Co":                      1.1,
    "Chou Associates Management":             1.1,
    "Sound Shore Management":                 1.1,
    "Lountzis Asset Management":              1.1,
    "Abrams Capital Management":              1.1,
    "Scion Asset Management (Burry)":         1.0,
    "Duquesne Family Office (Druckenmiller)": 1.0,
    "Third Point (Loeb)":                     1.0,
    "Trian Fund Management (Peltz)":          1.0,
    "Arbiter Partners Capital Management":    1.0,
    "Brave Warrior Advisors (Ainslie)":       1.0,
    "7G Capital Management":                  1.0,
    "SurgoCap Partners":                      1.0,
    "DME Capital Management":                 1.0,
    "Soros Fund Management (Soros)":          0.9,
    "Thiel Macro (Thiel)":                    0.9,
    "Appaloosa (Tepper)":                     0.9,
    "Lone Pine Capital (Mandel)":             0.9,
    "Whale Rock Capital Management":          0.9,
    "Atreides Management":                    0.9,
    "SoftBank Group Corp":                    0.7,
    "NVIDIA Corp":                            0.6,
    "Alphabet Inc":                           0.6,
    "Renaissance Technologies (Quant)":       0.3,
    "Two Sigma Investments (Quant)":          0.3,
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
# Tier 3, section 11-13, and needs a data source beyond the tracked filers
# in FILERS above). It approximates crowding from (a) how many of the
# tracked funds already hold the name, and (b) a static list of well-known
# "hedge fund hotel" mega caps that are structurally crowded regardless of
# what our tracked funds do.
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
# Predefined Call filters. A contract must pass ALL of them to be eligible;
# if none does, the engine reports NO_SUITABLE_OPTION_FOUND for that stock.
OPTION_MIN_DAYS        = 90     # expiry window (days to expiration)
OPTION_MAX_DAYS        = 180
OPTION_DELTA_MIN       = 0.30   # call delta window
OPTION_DELTA_MAX       = 0.70
OPTION_DELTA_TARGET    = 0.45   # selection prefers delta closest to this
OPTION_MAX_SPREAD_PCT  = 8.0    # (ask - bid) / mid
OPTION_MIN_VOLUME      = 300    # today's contract volume
OPTION_MIN_OPEN_INT    = 500    # open interest (always enforced)
# Daily volume resets every morning, so a chain pulled soon after the open
# under-reports it for every strike. A contract with at least this much open
# interest is treated as liquid even if today's volume is still below the
# floor above; open interest itself is never waived.
OPTION_OI_WAIVES_VOLUME = 2500
OPTION_MAX_IV          = 0.70   # skip overpriced premium (IV > 70%)
NO_SUITABLE_OPTION     = "NO_SUITABLE_OPTION_FOUND"

# ── Signal Engine (deterministic 0-100 model) ───────────────────────────────
# Final SIGNAL SCORE = Σ weight_i × factor_i  (factors are each 0-100, fixed
# absolute transforms - NOT universe-relative min-max - so a stock's score
# does not change just because a different set of peers was scored).
# Weights sum to 100. Crowding is a positive factor (LOW crowding = 100).
# A capped price-action penalty is subtracted afterwards (see below).
TOP_N = 10
SIGNAL_WEIGHTS = {
    "activity":        15,   # NEW / ADD activity strength
    "conviction":      15,   # portfolio weight / rank of the position
    "manager_quality": 15,   # dynamic manager quality of the buyers
    "accumulation":    10,   # multi-quarter build
    "consensus":       15,   # quality-weighted smart-money agreement
    "insider":         15,   # Form 4 open-market buying since quarter-end
    "freshness":       10,   # filing delay × turnover decay
    "crowding":         5,   # inverse crowding (LOW = 100, EXTREME = 0)
}
assert sum(SIGNAL_WEIGHTS.values()) == 100
SIGNAL_PRICE_PENALTY_CAP = 15.0   # max points removed for "already ran" names
# How many pre-ranked tickers get the (network-heavy) Form 4 look-up. Insider
# data can now move a name by up to 25 points (15 weight + 10 confluence), so a
# cut that is too tight hides exactly the setup the engine looks for. Each extra
# ticker costs roughly 40 EDGAR requests.
SIGNAL_CANDIDATE_POOL    = 60
CROWDING_FACTOR_BY_LABEL = {"LOW": 100.0, "MODERATE": 60.0, "HIGH": 25.0, "EXTREME": 0.0}

# ── Insider activity (SEC Form 4) ────────────────────────────────────────────
INSIDER_MAX_FORM4_PER_TICKER = 40     # newest Form 4s inspected per ticker
INSIDER_MIN_PURCHASE_USD     = 25_000 # ignore token-sized buys
INSIDER_CACHE_DIR            = BASE_DIR / "data" / "insider_cache"
INSIDER_CACHE_DIR.mkdir(parents=True, exist_ok=True)
# Sub-score anchors. The insider score is the sum of four capped components
# (value 40 + cluster 25 + role 25 + stake 10), minus a penalty that counts only
# DISCRETIONARY selling - a Rule 10b5-1 sale was scheduled months in advance and
# says nothing about today's conviction (SEC Form 4 flag <aff10b5One>).
INSIDER_VALUE_FULL_USD       = 2_000_000   # ≥ $2M of buying = full value credit
INSIDER_CLUSTER_FULL_COUNT   = 3           # ≥ 3 distinct insiders buying = full cluster credit
INSIDER_CLUSTER_WINDOW_DAYS  = 30          # buys this close together count as cluster buying
INSIDER_STAKE_FULL_PCT       = 25.0        # a buy lifting an insider's own stake by ≥25% = full credit
# Role weighting: the people closest to the numbers carry the most signal.
INSIDER_ROLE_POINTS = {"CEO": 25.0, "CFO": 25.0, "OFFICER": 18.0, "DIRECTOR": 12.0, "TEN_PCT": 6.0, "OTHER": 6.0}
INSIDER_DISCRETIONARY_SELL_PENALTY = 25.0  # max points removed for genuine discretionary selling

# ── Confluence: 13F accumulation confirmed by insider buying ─────────────────
# A weighted sum treats "great 13F, no insider" the same as "mediocre 13F, great
# insider". The bonus is an explicit interaction term for the setup the engine
# is actually looking for, and is capped so it can never dominate the ranking.
CONFLUENCE_MAX_BONUS      = 10.0
CONFLUENCE_MIN_13F_SCORE  = 55.0   # the 13F side must be strong on its own
CONFLUENCE_MIN_INSIDER    = 40.0   # and the insider side must be a real confirmation

# ── Claude API: cost-aware model routing & cascading ─────────────────────────
# Every LLM task is routed to the cheapest tier that historically passes
# validation; on validation failure the router escalates one tier
# (cascade). Responses are cached on disk by content hash, so re-running on
# identical data costs zero tokens and yields identical narratives.
LLM_MODELS = {
    "haiku":  {"id": "claude-haiku-4-5",  "in_per_mtok": 1.00, "out_per_mtok": 5.00,  "cache_read_per_mtok": 0.10},
    "sonnet": {"id": "claude-sonnet-5",   "in_per_mtok": 2.00, "out_per_mtok": 10.00, "cache_read_per_mtok": 0.20},
    "opus":   {"id": "claude-opus-5",     "in_per_mtok": 5.00, "out_per_mtok": 25.00, "cache_read_per_mtok": 0.50},
}
# task -> ordered cascade of tiers (cheapest first)
LLM_TASK_ROUTES = {
    "market_context":   ["haiku"],
    "explain_signals":  ["haiku", "sonnet", "opus"],
    "option_rationale": ["haiku", "sonnet"],
}
LLM_MAX_RUN_COST_USD = 1.50     # hard budget per pipeline run; beyond it -> rule-based fallback
LLM_CACHE_DIR        = BASE_DIR / "data" / "llm_cache"
LLM_CACHE_DIR.mkdir(parents=True, exist_ok=True)
CLAUDE_MAX_TOKENS    = 8192
CLAUDE_RETRY_COUNT   = 3
CLAUDE_RETRY_DELAY   = 5   # seconds

# ── Gmail ─────────────────────────────────────────────────────────────────────
GMAIL_SMTP_HOST = "smtp.gmail.com"
GMAIL_SMTP_PORT = 587
REPORT_SUBJECT  = "13F Signal Engine – Top 10 – {date}"
