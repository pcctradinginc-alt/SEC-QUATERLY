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
    score, reasons = ia.insider_score(s)
    # 55 * 502500/2M + 30 * 1/3 + 15 = 13.82 + 10 + 15 = 38.8
    assert score == 38.8
    assert any("open-market insider purchases" in r for r in reasons)
    assert ia.insider_score({}) == (0.0, ["No Form 4 open-market insider transactions since quarter-end"])


def test_net_sellers_are_capped():
    s = {"buy_count": 1, "buy_value_usd": 3_000_000, "sell_count": 3, "sell_value_usd": 50_000_000,
         "net_value_usd": -47_000_000, "distinct_buyers": ["A", "B", "C"], "officer_or_director_buyers": ["A"]}
    score, reasons = ia.insider_score(s)
    assert score == 20.0
    assert any("net sellers" in r for r in reasons)


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
