import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import insider_activity as ia  # noqa: E402
import llm_router  # noqa: E402

FORM4 = """<?xml version="1.0"?>
<ownershipDocument>
  <issuer><issuerCik>0000000001</issuerCik><issuerTradingSymbol>TST</issuerTradingSymbol></issuer>
  <reportingOwner>
    <reportingOwnerId><rptOwnerCik>0000000002</rptOwnerCik><rptOwnerName>DOE JANE</rptOwnerName></reportingOwnerId>
    <reportingOwnerRelationship><isDirector>0</isDirector><isOfficer>1</isOfficer><officerTitle>Chief Executive Officer</officerTitle></reportingOwnerRelationship>
  </reportingOwner>
  <nonDerivativeTable>
    <nonDerivativeTransaction>
      <securityTitle><value>Common Stock</value></securityTitle>
      <transactionDate><value>2026-08-20</value></transactionDate>
      <transactionCoding><transactionCode>P</transactionCode></transactionCoding>
      <transactionAmounts>
        <transactionShares><value>10000</value></transactionShares>
        <transactionPricePerShare><value>50.25</value></transactionPricePerShare>
        <transactionAcquiredDisposedCode><value>A</value></transactionAcquiredDisposedCode>
      </transactionAmounts>
    </nonDerivativeTransaction>
    <nonDerivativeTransaction>
      <securityTitle><value>Common Stock</value></securityTitle>
      <transactionDate><value>2026-05-01</value></transactionDate>
      <transactionCoding><transactionCode>P</transactionCode></transactionCoding>
      <transactionAmounts>
        <transactionShares><value>999999</value></transactionShares>
        <transactionPricePerShare><value>40</value></transactionPricePerShare>
        <transactionAcquiredDisposedCode><value>A</value></transactionAcquiredDisposedCode>
      </transactionAmounts>
    </nonDerivativeTransaction>
    <nonDerivativeTransaction>
      <securityTitle><value>Common Stock</value></securityTitle>
      <transactionDate><value>2026-08-21</value></transactionDate>
      <transactionCoding><transactionCode>G</transactionCode></transactionCoding>
      <transactionAmounts>
        <transactionShares><value>5000</value></transactionShares>
        <transactionPricePerShare><value>0</value></transactionPricePerShare>
        <transactionAcquiredDisposedCode><value>D</value></transactionAcquiredDisposedCode>
      </transactionAmounts>
    </nonDerivativeTransaction>
  </nonDerivativeTable>
</ownershipDocument>"""


FORM4_FOREIGN_ISSUER = FORM4.replace(
    "<issuerCik>0000000001</issuerCik>", "<issuerCik>0000000099</issuerCik>"
).replace("<issuerTradingSymbol>TST</issuerTradingSymbol>",
          "<issuerTradingSymbol>OTHER</issuerTradingSymbol>")

FORM4_PREFERRED = FORM4.replace("<value>Common Stock</value>",
                                "<value>Preferred Stock, Series DD</value>")


def test_issuer_is_parsed():
    """An issuer's EDGAR feed also holds Form 4s the company filed as an insider
    of a DIFFERENT issuer - Uber's Aurora sale, Berkshire's DaVita sale."""
    p = ia.parse_form4(FORM4)
    assert p["issuer"]["cik"] == "0000000001"
    assert p["issuer"]["symbol"] == "TST"
    assert ia.parse_form4(FORM4_FOREIGN_ISSUER)["issuer"]["cik"] == "0000000099"


def test_non_common_securities_are_excluded():
    assert ia.is_common_stock("Common Stock")
    assert ia.is_common_stock("Class A Common Stock")
    assert ia.is_common_stock("ADSs")
    assert not ia.is_common_stock("Preferred Stock, Series DD")
    assert not ia.is_common_stock("Mandatory Redeemable Preferred Shares, Series D")
    assert not ia.is_common_stock("Warrants")
    assert not ia.is_common_stock("6.375% Senior Notes")

    s = ia.summarize_form4s(
        [{"filing_date": "2026-08-22", "accession": "a", **ia.parse_form4(FORM4_PREFERRED)}],
        since="2026-06-30")
    assert s["buy_count"] == 0
    assert s["skipped_securities"] == {"Preferred Stock, Series DD": 2}


def test_net_stance():
    buy_only = ia.summarize_form4s(
        [{"filing_date": "2026-08-22", "accession": "a", **ia.parse_form4(FORM4)}], "2026-06-30")
    assert buy_only["net_stance"] == "NET_BUYING"
    assert ia.summarize_form4s([], "2026-06-30")["net_stance"] == "NO_ACTIVITY"


def test_parse_and_window():
    p = ia.parse_form4(FORM4)
    assert p["owners"][0]["is_officer"] and p["owners"][0]["title"] == "Chief Executive Officer"
    assert len(p["transactions"]) == 3
    s = ia.summarize_form4s([{"filing_date": "2026-08-22", "accession": "acc-1", **p}], since="2026-06-30")
    # the May purchase is before the window; the gift (G) is not a buy
    assert s["buy_count"] == 1
    assert s["buy_value_usd"] == 502500.0
    assert s["distinct_buyers"] == ["DOE JANE"]
    assert s["officer_or_director_buyers"] == ["DOE JANE"]


def test_insider_score_transform():
    s = ia.summarize_form4s([{"filing_date": "2026-08-22", "accession": "acc-1", **ia.parse_form4(FORM4)}], "2026-06-30")
    assert s["top_role"] == "CEO"
    score, reasons = ia.insider_score(s)
    # value 40*502500/2M=10.05 + cluster 25*1/3=8.33 + role CEO 25 + stake 0
    assert score == 43.4
    assert any("open-market insider purchases" in r for r in reasons)
    assert ia.insider_score({}) == (0.0, ["No Form 4 open-market insider transactions since quarter-end"])


def test_seniority_is_weighted():
    """A CEO buying carries more signal than a director buying the same amount."""
    base = dict(buy_count=1, buy_value_usd=500_000, sell_count=0, sell_value_usd=0,
                discretionary_sell_value_usd=0, planned_sell_value_usd=0,
                distinct_buyers=["A"], officer_or_director_buyers=["A"],
                cluster_buying=False, max_stake_change_pct=0.0)
    ceo = ia.insider_score({**base, "top_role": "CEO"})[0]
    officer = ia.insider_score({**base, "top_role": "OFFICER"})[0]
    director = ia.insider_score({**base, "top_role": "DIRECTOR"})[0]
    assert ceo > officer > director


def test_planned_10b5_1_sales_do_not_count_against_the_signal():
    """Carvana's insiders sold under pre-arranged plans; that is not a bearish
    discretionary decision and must not be scored like one."""
    common = dict(buy_count=1, buy_value_usd=1_500_000, sell_count=5, sell_value_usd=27_000_000,
                  distinct_buyers=["A"], officer_or_director_buyers=["A"], top_role="DIRECTOR",
                  cluster_buying=False, max_stake_change_pct=0.0)
    planned = ia.insider_score({**common, "planned_sell_value_usd": 27_000_000,
                                "discretionary_sell_value_usd": 0})[0]
    discretionary = ia.insider_score({**common, "planned_sell_value_usd": 0,
                                      "discretionary_sell_value_usd": 27_000_000})[0]
    assert planned > discretionary
    assert abs(discretionary - max(0.0, planned - 25.0)) < 0.05   # full penalty, nothing else changed
    reasons = ia.insider_score({**common, "planned_sell_value_usd": 27_000_000,
                                "discretionary_sell_value_usd": 0})[1]
    assert any("Rule 10b5-1" in r for r in reasons)


def test_cluster_buying_and_stake_change():
    tight = {"filing_date": "2026-08-10", "accession": "a", **ia.parse_form4(FORM4)}
    second = ia.parse_form4(FORM4.replace("DOE JANE", "ROE RICHARD")
                            .replace("<rptOwnerCik>0000000002</rptOwnerCik>",
                                     "<rptOwnerCik>0000000003</rptOwnerCik>"))
    s = ia.summarize_form4s([tight, {"filing_date": "2026-08-12", "accession": "b", **second}], "2026-06-30")
    assert s["cluster_buying"] is True
    assert len(s["cluster_buyers"]) == 2

    far = ia.parse_form4(FORM4.replace("DOE JANE", "ROE RICHARD")
                         .replace("<rptOwnerCik>0000000002</rptOwnerCik>",
                                  "<rptOwnerCik>0000000003</rptOwnerCik>")
                         .replace("2026-08-20", "2026-08-01"))
    s2 = ia.summarize_form4s([tight, {"filing_date": "2026-08-01", "accession": "b", **far}], "2026-06-30")
    assert s2["cluster_buying"] is True   # 19 days apart is still a cluster


def test_stake_change_is_computed():
    xml = FORM4.replace("""        <transactionAcquiredDisposedCode><value>A</value></transactionAcquiredDisposedCode>
      </transactionAmounts>
    </nonDerivativeTransaction>
    <nonDerivativeTransaction>
      <securityTitle><value>Common Stock</value></securityTitle>
      <transactionDate><value>2026-05-01</value></transactionDate>""",
    """        <transactionAcquiredDisposedCode><value>A</value></transactionAcquiredDisposedCode>
      </transactionAmounts>
      <postTransactionAmounts><sharesOwnedFollowingTransaction><value>50000</value></sharesOwnedFollowingTransaction></postTransactionAmounts>
    </nonDerivativeTransaction>
    <nonDerivativeTransaction>
      <securityTitle><value>Common Stock</value></securityTitle>
      <transactionDate><value>2026-05-01</value></transactionDate>""")
    s = ia.summarize_form4s([{"filing_date": "2026-08-22", "accession": "a", **ia.parse_form4(xml)}], "2026-06-30")
    # bought 10,000 and ended with 50,000 -> stake up 25% on a 40,000 base
    assert s["max_stake_change_pct"] == 25.0


def test_router_cascades_and_caches(monkeypatch, tmp_path):
    """Haiku returns invalid output → router escalates to Sonnet; second call is a cache hit."""
    monkeypatch.setattr(llm_router, "LLM_CACHE_DIR", tmp_path)
    monkeypatch.setattr(llm_router, "_BUDGET", llm_router.RunBudget(max_usd=5.0))
    calls = []

    def fake_call(model_key, system, user, tool, max_tokens):
        calls.append(model_key)
        usage = {"input_tokens": 1000, "output_tokens": 200, "cache_read": 0, "cache_write": 0}
        if model_key == "haiku":
            return {"answer": "bad"}, usage
        return {"answer": "good"}, usage

    monkeypatch.setattr(llm_router, "_call_once", fake_call)
    validator = lambda d: (d.get("answer") == "good", "answer")
    tool = {"name": "t", "input_schema": {"type": "object"}}

    out, meta = llm_router.route("explain_signals", "sys", "user", tool, validator)
    assert out == {"answer": "good"}
    assert calls == ["haiku", "sonnet"]
    assert meta["model"] == "claude-sonnet-5" and meta["escalated_from"] == "haiku"
    assert meta["cost_usd"] > 0

    out2, meta2 = llm_router.route("explain_signals", "sys", "user", tool, validator)
    assert out2 == out and meta2["cached"] is True
    assert calls == ["haiku", "sonnet"]          # no new API calls
    assert llm_router.budget().cache_hits == 1


def test_router_respects_budget(monkeypatch, tmp_path):
    monkeypatch.setattr(llm_router, "LLM_CACHE_DIR", tmp_path)
    monkeypatch.setattr(llm_router, "_BUDGET", llm_router.RunBudget(max_usd=0.0))
    monkeypatch.setattr(llm_router, "_call_once", lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not call")))
    out, meta = llm_router.route("explain_signals", "sys", "user", {"name": "t", "input_schema": {}}, None)
    assert out is None
    assert meta["attempts"][0]["skipped"] == "budget"


FORM4_AMENDED = FORM4.replace("<documentType>4</documentType>", "<documentType>4/A</documentType>") \
    if "<documentType>4</documentType>" in FORM4 else FORM4


def test_amendment_does_not_double_count_a_transaction():
    """A Form 4/A restates the original and repeats its transactions. Summing
    both would turn one purchase into two - worth up to 25 score points."""
    original = {"filing_date": "2026-08-22", "accession": "acc-1", "form": "4",
                **ia.parse_form4(FORM4)}
    amendment = {"filing_date": "2026-08-25", "accession": "acc-2", "form": "4/A",
                 **ia.parse_form4(FORM4_AMENDED)}

    alone = ia.summarize_form4s([original], "2026-06-30")
    both = ia.summarize_form4s([original, amendment], "2026-06-30")

    assert both["buy_count"] == alone["buy_count"] == 1
    assert both["gross_buy_value_usd"] == alone["gross_buy_value_usd"] == 502500.0
    # every in-window row of the amendment is recognised as a repeat, not just the buy
    assert both["superseded_transactions"] == 2
    assert ia.insider_score(both)[0] == ia.insider_score(alone)[0]


def test_only_significant_buys_drive_the_value_score():
    """A hundred token-sized trades must not score like one conviction buy."""
    tiny = []
    for i in range(20):
        p = ia.parse_form4(FORM4.replace("<value>10000</value>", "<value>100</value>")
                           .replace(f"<rptOwnerCik>0000000002</rptOwnerCik>",
                                    f"<rptOwnerCik>00000000{i:02d}</rptOwnerCik>")
                           .replace("2026-08-20", f"2026-08-{i % 28 + 1:02d}"))
        tiny.append({"filing_date": "2026-08-22", "accession": f"a{i}", "form": "4", **p})
    s = ia.summarize_form4s(tiny, "2026-06-30")
    assert s["gross_buy_value_usd"] > 0                    # reported
    assert s["significant_buy_value_usd"] == 0.0           # but not scored
    assert ia.insider_score(s)[0] < 30.0
