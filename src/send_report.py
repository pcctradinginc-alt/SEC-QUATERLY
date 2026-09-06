"""
send_report.py
Renders the Top-10 signal report as a clean, responsive HTML e-mail
(minimalist, Apple-style: generous whitespace, SF system font stack,
hairline dividers, restrained colour) and sends it via Gmail.

Layout is table-based with inline styles for e-mail-client compatibility;
a small <style> block adds mobile tweaks for clients that honour it.
"""

import html
import json
import re
import os
import smtplib
from datetime import date
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from config import (
    run_date,
    DATA_DIR, GMAIL_SMTP_HOST, GMAIL_SMTP_PORT, NO_SUITABLE_OPTION,
    REPORT_SUBJECT, REPORTS_DIR, SIGNAL_WEIGHTS,
)

# ── palette (Apple HIG-inspired) ─────────────────────────────────────────────
INK      = "#1d1d1f"
INK2     = "#6e6e73"
INK3     = "#86868b"
LINE     = "#e5e5ea"
CARD     = "#ffffff"
BG       = "#f5f5f7"
BLUE     = "#0071e3"
GREEN    = "#34c759"
ORANGE   = "#ff9500"
RED      = "#ff3b30"
PURPLE   = "#5e5ce6"

FONT = "-apple-system,BlinkMacSystemFont,'SF Pro Text','Helvetica Neue',Helvetica,Arial,sans-serif"
MONO = "'SF Mono',SFMono-Regular,Menlo,Consolas,monospace"

FACTOR_ORDER = ["activity", "conviction", "manager_quality", "consensus",
                "insider", "accumulation", "freshness", "crowding"]
FACTOR_LABELS = {
    "activity": "NEW / ADD activity", "conviction": "Portfolio conviction",
    "manager_quality": "Manager quality", "accumulation": "Multi-quarter accumulation",
    "consensus": "Smart-money consensus", "insider": "Insider buying (Form 4)",
    "freshness": "Filing freshness", "crowding": "Low crowding",
}


def esc(x) -> str:
    return html.escape(str(x if x is not None else ""))


_DANGLING_SUFFIX_RE = re.compile(r"[\s,]+(FORMERLY|FKA|F/K/A)\s*$", re.I)


def clean_issuer_name(name: str) -> str:
    """
    13F filers type the issuer name by hand and often truncate it: Elevance
    arrives as "ELEVANCE HEALTH INC FORMERLY" (from "... FORMERLY ANTHEM INC").
    Drop a trailing "formerly" that names nothing; leave everything else alone.
    """
    return _DANGLING_SUFFIX_RE.sub("", (name or "").strip()).strip()


def grade_color(grade: str) -> str:
    return {"VERY_STRONG": GREEN, "STRONG": BLUE, "MODERATE": ORANGE, "WEAK": INK3}.get(grade, INK3)


def pill(text: str, color: str, bg: str | None = None) -> str:
    bg = bg or f"{color}1a"
    return (f'<span style="display:inline-block;padding:3px 10px;border-radius:999px;font-size:11px;'
            f'font-weight:600;letter-spacing:.02em;color:{color};background:{bg};margin:0 6px 6px 0">{esc(text)}</span>')


def bar(label: str, value: float, weight: int, points: float) -> str:
    width = max(2, int(round(value)))
    return f"""
      <tr>
        <td style="padding:5px 0;font-size:12px;color:{INK2};width:44%">{esc(label)}</td>
        <td style="padding:5px 8px;width:36%">
          <div style="background:{LINE};border-radius:4px;height:6px;overflow:hidden">
            <div style="width:{width}%;height:6px;background:{BLUE};border-radius:4px"></div>
          </div>
        </td>
        <td style="padding:5px 0;font-size:12px;color:{INK};text-align:right;white-space:nowrap">
          <span style="font-weight:600">{points:.1f}</span><span style="color:{INK3}"> / {weight}</span>
        </td>
      </tr>"""


def section_title(text: str, sub: str = "") -> str:
    sub_html = f'<div style="font-size:13px;color:{INK2};margin-top:4px">{esc(sub)}</div>' if sub else ""
    return (f'<div style="margin:36px 0 14px"><div style="font-size:20px;font-weight:600;color:{INK};'
            f'letter-spacing:-.01em">{esc(text)}</div>{sub_html}</div>')


# ── blocks ────────────────────────────────────────────────────────────────────

def summary_table(top: list[dict]) -> str:
    rows = ""
    for s in top:
        o = s.get("option") or {}
        c = o.get("contract")
        if c:
            opt = f'<span class="mono" style="font-family:{MONO};font-size:11px">{esc(c["symbol"])}</span>'
        elif o.get("status") == "NOT_EVALUATED":
            opt = f'<span style="color:{INK3};font-size:11px">not evaluated</span>'
        else:
            opt = f'<span style="color:{INK3};font-size:11px">no Call</span>'
        ins = s["factors"].get("insider", 0)
        stance = ((s.get("insider") or {}).get("summary") or {}).get("net_stance")
        if stance == "NET_BUYING":
            ins_html = f'<span style="color:{GREEN};font-weight:600">{ins:.0f}</span>'
        elif stance == "NET_SELLING":
            ins_html = f'<span style="color:{ORANGE}">{ins:.0f} sell</span>'
        else:
            ins_html = f'<span style="color:{INK3}">–</span>' 
        rows += f"""
        <tr>
          <td class="r" style="padding:10px 0;font-size:13px;color:{INK3};width:28px">{s['rank']}</td>
          <td class="r" style="padding:10px 6px;font-size:14px;font-weight:600;color:{INK}">{esc(s['ticker'])}
            <div style="font-size:11px;color:{INK3};font-weight:400">{esc(clean_issuer_name(s['name'])[:34])}</div></td>
          <td class="r" style="padding:10px 6px;font-size:15px;font-weight:600;color:{grade_color(s['grade'])};text-align:right">{s['signal_score']:.0f}</td>
          <td class="r" style="padding:10px 6px;font-size:12px;text-align:center">{ins_html}</td>
          <td class="r" style="padding:10px 0 10px 6px;text-align:right;word-break:break-all">{opt}</td>
        </tr>"""
    return f"""
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse">
      <tr class="k">
        <td style="padding:0 0 6px">#</td><td style="padding:0 6px 6px">Stock</td>
        <td style="padding:0 6px 6px;text-align:right">Score</td>
        <td style="padding:0 6px 6px;text-align:center">Insider</td>
        <td style="padding:0 0 6px 6px;text-align:right">Call</td>
      </tr>{rows}
    </table>"""


def buyers_table(filers: list[dict]) -> str:
    rows = ""
    for f in sorted(filers, key=lambda r: -(r.get("port_weight_pct") or 0)):
        d = f.get("delta_pct")
        chg = "NEW" if f.get("delta_type") == "NEW" else (f"{d:+.0f}%" if d is not None else "")
        chg_color = GREEN if f.get("delta_type") == "NEW" else INK
        rows += f"""
        <tr>
          <td class="r" style="padding:6px 0;font-size:12px;color:{INK}">{esc(f['filer'])}</td>
          <td class="r" style="padding:6px 6px;font-size:12px;color:{INK};text-align:right">{(f.get('port_weight_pct') or 0):.1f}%</td>
          <td class="r" style="padding:6px 6px;font-size:12px;color:{chg_color};text-align:right;font-weight:600">{esc(chg)}</td>
          <td class="r" style="padding:6px 0 6px 6px;font-size:12px;color:{INK2};text-align:right">{(f.get('manager_quality_score') or 0):.2f}{'<span style="color:' + INK3 + '"> ~</span>' if f.get('manager_quality_source') == 'BOOTSTRAPPED' else ''}</td>
        </tr>"""
    return f"""
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse; margin-top:6px">
      <tr class="k">
        <td style="padding:0 0 4px">Manager</td><td style="padding:0 6px 4px;text-align:right">Weight</td>
        <td style="padding:0 6px 4px;text-align:right">Change</td><td style="padding:0 0 4px 6px;text-align:right">Quality</td>
      </tr>{rows}
    </table>
    <div style="font-size:11px;color:{INK3};margin-top:4px">
      Quality marked ~ is still bootstrapped from a prior rather than measured over enough quarters.
    </div>""" if any(f.get("manager_quality_source") == "BOOTSTRAPPED" for f in filers) else f"""
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0" class="tb" style="border-collapse:collapse;margin-top:6px">
      <tr class="k">
        <td style="padding:0 0 4px">Manager</td><td style="padding:0 6px 4px;text-align:right">Weight</td>
        <td style="padding:0 6px 4px;text-align:right">Change</td><td style="padding:0 0 4px 6px;text-align:right">Quality</td>
      </tr>{rows}
    </table>"""


def insider_block(s: dict) -> str:
    ins  = s.get("insider") or {}
    summ = ins.get("summary") or {}
    buy_val  = summ.get("buy_value_usd", 0) or 0
    sell_val = summ.get("sell_value_usd", 0) or 0
    stance   = summ.get("net_stance")
    read = (s.get("commentary") or {}).get("insider_read", "")

    # The headline must state the NET stance. Reporting "insider buying
    # confirmed" for a name whose insiders sold more than they bought reads as
    # confirmation of the 13F signal when the data says the opposite.
    if stance == "NET_BUYING":
        head_color = GREEN
        head = f"Net insider buying · ${buy_val:,.0f} bought"
        if sell_val:
            head += f" vs ${sell_val:,.0f} sold"
    elif stance == "NET_SELLING":
        head_color = ORANGE
        head = f"Net insider selling · ${sell_val:,.0f} sold"
        head += f" vs ${buy_val:,.0f} bought" if buy_val else " · no purchases"
    elif stance == "BALANCED":
        head_color = INK2
        head = f"Insider buying and selling balanced · ${buy_val:,.0f} each way"
    elif ins.get("error"):
        head_color, head = INK3, "Insider data unavailable"
    else:
        head_color, head = INK3, "No open-market insider transactions"

    rows = ""
    for b in (summ.get("buys") or [])[-4:][::-1]:
        rows += (f'<div style="font-size:12px;color:{INK2};margin-top:4px">'
                 f'<span style="color:{GREEN};font-weight:600">BUY</span> {esc(b["date"])} · '
                 f'{esc(b["insider"][:38])} ({esc(b["role"][:26])}) · '
                 f'{b["shares"]:,.0f} sh @ ${b["price"]:,.2f} = '
                 f'<span style="color:{INK};font-weight:600">${b["value_usd"]:,.0f}</span></div>')
    for x in (summ.get("sells") or [])[-2:][::-1]:
        rows += (f'<div style="font-size:12px;color:{INK2};margin-top:4px">'
                 f'<span style="color:{ORANGE};font-weight:600">SELL</span> {esc(x["date"])} · '
                 f'{esc(x["insider"][:38])} ({esc(x["role"][:26])}) · '
                 f'{x["shares"]:,.0f} sh @ ${x["price"]:,.2f} = ${x["value_usd"]:,.0f}</div>')

    # Transparency: say when filings were excluded, and why.
    notes = []
    planned = summ.get("planned_sell_value_usd", 0) or 0
    if planned:
        notes.append(f"${planned:,.0f} of the sales ran under pre-arranged Rule 10b5-1 plans "
                     f"and are not scored as a bearish signal")
    disc = summ.get("discretionary_sell_value_usd")
    if planned and disc:
        notes.append(f"${disc:,.0f} was discretionary selling")
    foreign = ins.get("foreign_issuer_skipped") or {}
    if foreign:
        notes.append(f"{sum(foreign.values())} filing(s) excluded: this company reporting as an "
                     f"insider of {', '.join(sorted(foreign)[:4])}, not trades in its own stock")
    skipped = summ.get("skipped_securities") or {}
    if skipped:
        notes.append(f"{sum(skipped.values())} non-common-stock line(s) excluded "
                     f"({', '.join(sorted(skipped)[:2])})")
    notes_html = "".join(
        f'<div style="font-size:11px;color:{INK3};margin-top:6px">{esc(n)}</div>' for n in notes)

    return f"""
    <div style="border:1px solid {LINE};border-radius:12px;padding:14px 16px;margin-top:14px">
      <div class="k2">SEC Form 4 · common stock · since {esc(ins.get('since', 'quarter-end'))}</div>
      <div style="font-size:14px;font-weight:600;color:{head_color};margin-top:4px">{esc(head)}</div>
      {rows}
      {f'<div style="font-size:12px;color:{INK2};margin-top:8px">{esc(read)}</div>' if read else ''}
      {notes_html}
    </div>"""


def quote_note(s: dict) -> str:
    """Say plainly whether the option quotes were taken with the market open."""
    m = (s.get("_market") or {})
    state = (m.get("state") or "").lower()
    if state == "open":
        return "Live quotes from the analysis run. Verify before trading."
    if state:
        return (f"Market {state.upper()} at the time of the run"
                f"{' · ' + m['description'] if m.get('description') else ''} – "
                f"volume reflects the last session, not live trading. Verify before trading.")
    return "Delayed snapshot from the analysis run. Verify before trading."


def option_block(s: dict) -> str:
    o = s.get("option") or {}
    c = o.get("contract")
    # Deterministic, from the selected contract - never model-generated.
    note = o.get("rule_rationale", "")
    if not c:
        rej = o.get("rejections") or {}
        rej_txt = ", ".join(f"{k} {v}" for k, v in rej.items()) if rej else "no chain inside the expiry window"
        if o.get("status") == "NOT_EVALUATED":
            body = "Option chain not evaluated in this run (no Tradier key)."
        else:
            body = ("No option currently satisfies the minimum liquidity, delta and "
                    f"spread requirements · rejected on: {rej_txt}.")
        return f"""
    <div style="background:{BG};border-radius:12px;padding:16px;margin-top:14px">
      <div class="k2">Call option</div>
      <div style="font-family:{MONO};font-size:14px;font-weight:600;color:{INK};margin-top:4px">{esc(o.get('status') if o.get('status') != 'NOT_EVALUATED' else 'NOT_EVALUATED')}</div>
      <div style="font-size:12px;color:{INK2};margin-top:6px">{esc(body)}</div>
      {f'<div style="font-size:12px;color:{INK2};margin-top:6px">{esc(note)}</div>' if note else ''}
    </div>"""

    iv = o.get("iv_metrics") or {}
    iv_txt = f" · IV rank {iv.get('iv_rank')}" if iv.get("iv_rank") is not None else ""
    money = c.get("moneyness_pct")
    money_txt = f"{money:+.1f}% vs spot" if money is not None else ""
    spot = (s.get("option") or {}).get("current_price")
    cells = [
        ("Stock now", f"${spot:,.2f}" if spot else "n/a", "underlying"),
        ("Strike", f"${c['strike']:g}", money_txt),
        ("Expiry", esc(c["expiration"]), f"{c['dte']} days"),
        ("Delta", f"{c['delta']:.2f}", (f"IV {c['implied_volatility']:.0%}" if c.get("implied_volatility") else "")),
        ("Bid / Ask", f"${c['bid']:.2f} / ${c['ask']:.2f}", f"spread {c['spread_pct']}%"),
        ("Mid", f"${c['mid']:.2f}", "entry reference"),
        ("Liquidity", f"{c['volume']:,} / {c['open_interest']:,}", "vol / OI"),
        ("Max risk", f"${c['max_risk_per_contract']:,.0f}", f"BE ${c['breakeven']:,.2f}"),
    ]
    tds = [f'<td class="cell" style="padding:8px 8px 8px 0;vertical-align:top;width:33%">'
           f'<div class="k">{esc(k)}</div>'
           f'<div style="font-size:15px;font-weight:600;color:{INK};margin-top:2px">{v}</div>'
           f'<div style="font-size:11px;color:{INK3}">{esc(sub)}</div></td>' for k, v, sub in cells]
    return f"""
    <div style="background:{BG};border-radius:12px;padding:16px;margin-top:14px">
      <div class="k2">Call option · long call{iv_txt}</div>
      <div style="font-family:{MONO};font-size:15px;font-weight:600;color:{INK};margin-top:4px">{esc(c['symbol'])}</div>
      <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse; margin-top:6px">
        <tr>{''.join(tds[:3])}</tr><tr>{''.join(tds[3:])}</tr>
      </table>
      {f'<div style="font-size:12px;color:{INK2};margin-top:8px;line-height:1.5">{esc(note)}</div>' if note else ''}
      <div style="font-size:11px;color:{INK3};margin-top:8px">{esc(quote_note(s))}</div>
    </div>"""


def stock_card(s: dict) -> str:
    c = s.get("commentary") or {}
    why = c.get("why_strongest") or s["why"]["summary"]
    verdict = c.get("verdict")
    perf = s.get("post_filing_perf") or {}
    pct = perf.get("pct_change")
    perf_pill = ""
    if pct is not None:
        col = RED if pct >= 25 else ORANGE if pct >= 15 else GREEN
        perf_pill = pill(f"{pct:+.0f}% since quarter-end", col)

    bullets = "".join(f'<li style="margin:4px 0">{esc(b)}</li>' for b in s["why"]["bullets"][:4])
    risks = "".join(f'<li style="margin:4px 0">{esc(r)}</li>' for r in (c.get("risks") or [])[:3])
    bars = "".join(
        bar(FACTOR_LABELS[k], s["factors"][k], SIGNAL_WEIGHTS[k], s["contributions"][k]) for k in FACTOR_ORDER
    )
    penalty = s.get("price_penalty") or 0
    penalty_row = (f'<tr><td colspan="3" style="padding:6px 0 0;font-size:12px;color:{RED}">'
                   f'Price-action penalty −{penalty:.0f}</td></tr>' if penalty else "")

    return f"""
    <div class="card" style="background:{CARD};border:1px solid {LINE};border-radius:18px;padding:24px;margin-bottom:18px">
      <table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr>
        <td style="vertical-align:top">
          <div style="font-size:12px;color:{INK3}">No. {s['rank']}</div>
          <div style="font-size:26px;font-weight:700;color:{INK};letter-spacing:-.02em;margin-top:2px">{esc(s['ticker'])}</div>
          <div style="font-size:13px;color:{INK2};margin-top:2px">{esc(clean_issuer_name(s['name']))}</div>
        </td>
        <td style="vertical-align:top;text-align:right;white-space:nowrap">
          <div style="font-size:34px;font-weight:700;color:{grade_color(s['grade'])};letter-spacing:-.03em;line-height:1">{s['signal_score']:.0f}</div>
          <div style="font-size:10px;color:{INK3};text-transform:uppercase;letter-spacing:.08em;margin-top:4px">Signal score</div>
          <div style="font-size:11px;color:{INK3};margin-top:3px;white-space:nowrap">13F {s.get('score_13f', 0):.0f} · insider {s.get('insider_score', 0):.0f}{f" · +{s['confluence_bonus']:.0f} confluence" if s.get('confluence_bonus') else ""}</div>
        </td>
      </tr></table>

      <div style="margin-top:14px">
        {pill(s.get('signal_label') or s['grade'].replace('_', ' '), PURPLE) if s.get('signal_label') else ''}
        {pill(s['grade'].replace('_', ' '), grade_color(s['grade']))}
        {pill(f"Crowding {s.get('crowding_label') or '–'}", INK2)}
        {pill(f"{s['filer_count']} buyer{'s' if s['filer_count'] != 1 else ''}", INK2)}
        {pill("also " + ", ".join(a["ticker"] for a in s["same_issuer_alternates"]), INK2) if s.get("same_issuer_alternates") else ""}
        {pill(f"Verdict: {verdict.replace('_', ' ').title()}", PURPLE) if verdict else ''}
        {perf_pill}
      </div>

      <div class="k2" style="margin-top:16px">Why it qualifies</div>
      <div style="font-size:15px;line-height:1.55;color:{INK};margin-top:6px">{esc(why)}</div>
      <ul style="margin:8px 0 0;padding-left:18px;font-size:13px;color:{INK2};line-height:1.5">{bullets}</ul>

      <div class="k2" style="margin-top:18px">Score breakdown</div>
      <table role="presentation" width="100%" cellpadding="0" cellspacing="0" class="tb" style="border-collapse:collapse;margin-top:4px">{bars}{penalty_row}</table>

      <div class="k2" style="margin-top:18px">13F buyers</div>
      {buyers_table(s['filers'])}

      {insider_block(s)}
      {option_block(s)}

      {f'<div class="k2" style="margin-top:16px">Risks and possible misreads</div><ul style="margin:6px 0 0;padding-left:18px;font-size:13px;color:{INK2};line-height:1.5">{risks}</ul>' if risks else ''}
    </div>"""


def sell_block(sell_signals: list[dict]) -> str:
    if not sell_signals:
        return ""
    rows = ""
    for s in sell_signals[:8]:
        detail = (f"exited former #{s.get('prior_rank', '?')} position" if s.get("type") == "EXIT"
                  else f"reduced {s.get('delta_pct', 0):.0f}%")
        rows += (f'<tr><td class="r" style="padding:7px 0;font-size:13px;font-weight:600;color:{INK}">{esc(s.get("ticker"))}</td>'
                 f'<td class="r" style="padding:7px 6px;font-size:12px;color:{INK2}">{esc(s.get("filer"))}</td>'
                 f'<td class="r" style="padding:7px 0;font-size:12px;color:{INK2};text-align:right">{esc(detail)}</td></tr>')
    return section_title("Notable exits and reductions", "Informational only – not part of the Top 10") + \
        f'<div style="background:{CARD};border:1px solid {LINE};border-radius:18px;padding:18px 24px"><table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse">{rows}</table></div>'


def track_record_block(bt: dict | None) -> str:
    """Running record against the S&P 500, shown on every report."""
    s = (bt or {}).get("summary") or {}
    done = s.get("completed_90d") or 0
    if not done:
        return (f'<div style="font-size:11px;color:{INK3};margin-top:18px;padding:0 4px">'
                f'Track record: no signal has completed its 90-day window yet.</div>')

    ret, bench = s.get("avg_return_90d_pct"), s.get("avg_benchmark_90d_pct")
    excess, beat = s.get("avg_excess_90d_pct"), s.get("beat_benchmark_90d_pct")
    col = GREEN if (excess or 0) > 0 else ORANGE
    eng = (s.get("by_engine") or {})
    eng_txt = " · ".join(
        f"{'current engine' if k.startswith('v2') else 'legacy engine'}: "
        f"{v['signals']} signals, {v.get('avg_excess_90d_pct', 0) or 0:+.1f}% vs {esc(s.get('benchmark', 'SPY'))}"
        for k, v in sorted(eng.items()))

    return f"""
    <div style="background:{CARD};border:1px solid {LINE};border-radius:18px;padding:18px 24px;margin-top:18px">
      <div class="k2">Track record · 90 days · vs {esc(s.get('benchmark', 'SPY'))}</div>
      <div style="font-size:14px;color:{INK};margin-top:6px">
        <span style="font-weight:600;color:{col}">{excess:+.1f}% excess</span> on average
        ({ret:+.1f}% signal vs {bench:+.1f}% benchmark) across {done} completed signals ·
        {beat if beat is not None else '–'}% beat the benchmark
      </div>
      {f'<div style="font-size:11px;color:{INK3};margin-top:6px">{esc(eng_txt)}</div>' if eng_txt else ''}
      <div style="font-size:11px;color:{INK3};margin-top:6px">
        Stock returns, not option returns. The current engine has only just begun its forward record;
        legacy rows come from the earlier LLM-selected top-5 pipeline and are not evidence for it.
      </div>
    </div>"""


def methodology_block(a: dict) -> str:
    w = a.get("weights", SIGNAL_WEIGHTS)
    wrows = "".join(
        f'<tr><td style="padding:4px 0;font-size:12px;color:{INK2}">{esc(FACTOR_LABELS[k])}</td>'
        f'<td style="padding:4px 0;font-size:12px;color:{INK};text-align:right">{v}</td></tr>'
        for k, v in w.items()
    )
    filters = a.get("filters") or {}
    frows = "".join(f'<li style="margin:3px 0">{esc(v)}</li>' for v in filters.values())
    llm = a.get("llm") or {}
    src = a.get("commentary_source") or "rule-based"
    route = " → ".join(f"{x.get('tier')}{' ✓' if x.get('ok') or x.get('cached') else ''}" for x in llm.get("route", []) if x.get("tier"))
    return section_title("Methodology") + f"""
    <div style="background:{CARD};border:1px solid {LINE};border-radius:18px;padding:22px 24px">
      <table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr>
        <td class="stack" style="vertical-align:top;width:50%;padding-right:12px">
          <div class="k2">Signal score weights (0–100)</div>
          <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="margin-top:6px">{wrows}</table>
          <div style="font-size:11px;color:{INK3};margin-top:8px">Each factor is a fixed 0–100 transform; identical inputs always produce the identical ranking. A capped price-action penalty is subtracted for names that already ran.</div>
        </td>
        <td class="stack" style="vertical-align:top;width:50%;padding-left:12px">
          <div class="k2">Call option filters</div>
          <ul style="margin:6px 0 0;padding-left:18px;font-size:12px;color:{INK2}">{frows or '<li>not evaluated</li>'}</ul>
          <div style="font-size:11px;color:{INK3};margin-top:8px">One Call per stock; if none passes every filter the report says {esc(NO_SUITABLE_OPTION)}.</div>
        </td>
      </tr></table>
      <div style="border-top:1px solid {LINE};margin-top:16px;padding-top:12px;font-size:11px;color:{INK3};line-height:1.6">
        Commentary: {esc(src)}{f' · route {esc(route)}' if route else ''} · API calls {llm.get('api_calls', 0)} · cache hits {llm.get('cache_hits', 0)} · est. cost ${llm.get('spent_usd', 0):.4f}<br>
        Input fingerprint <span style="font-family:{MONO}">{esc((a.get('input_fingerprint') or '')[:16])}</span> · ranking fingerprint <span style="font-family:{MONO}">{esc((a.get('ranking_fingerprint') or '')[:16])}</span>
      </div>
    </div>"""


# ── page ──────────────────────────────────────────────────────────────────────

def _hoist_repeated_styles(doc: str, min_count: int = 6) -> str:
    """E-mail size guard: inline style values repeated ≥ min_count times become
    classes in the <style> block (Gmail clips messages above ~100 KB)."""
    from collections import Counter
    counts = Counter(re.findall(r' style="([^"]{20,})"', doc))
    rules, mapping = [], {}
    for i, (val, n) in enumerate(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))):
        if n < min_count:
            break
        cls = f"s{i}"
        mapping[val] = cls
        rules.append(f".{cls}{{{val}}}")
    if not rules:
        return doc

    def repl(m):
        cls = mapping.get(m.group(3))
        if cls is None:
            return m.group(0)
        existing = m.group(2)
        return f' class="{existing} {cls}"' if existing else f' class="{cls}"'

    doc = re.sub(r'( class="([^"]*)")? style="([^"]{20,})"', repl, doc)
    return doc.replace("</style>", "\n" + "\n".join(rules) + "\n</style>", 1)


def _minify(doc: str) -> str:
    doc = re.sub(r">\s+<", "><", doc)
    doc = re.sub(r"\n\s*\n", "\n", doc)
    doc = re.sub(r"[ \t]{2,}", " ", doc)
    return _hoist_repeated_styles(doc)


def generate_html_report(a: dict) -> str:
    return _minify(_generate_html_report(a))


def _generate_html_report(a: dict) -> str:
    top = a.get("top10", [])
    today_str = a["date"]
    market = a.get("market") or {}
    for _s in top:
        _s["_market"] = market
    cards = "".join(stock_card(s) for s in top)
    n_ins = sum(1 for s in top
                if ((s.get("insider") or {}).get("summary") or {}).get("net_stance") == "NET_BUYING")
    quarter = a.get("quarter_label") or ""
    scored_n = a.get("stocks_scored")
    n_opt = sum(1 for s in top if (s.get("option") or {}).get("contract"))
    evaluated = any((s.get("option") or {}).get("status") != "NOT_EVALUATED" for s in top)
    opt_line = f"{n_opt} of 10 with a qualifying Call" if evaluated else "options not evaluated"
    ctx = a.get("market_context", "")

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta name="color-scheme" content="light">
<title>13F Signal Engine – {esc(today_str)}</title>
<style>
  body {{ margin:0; padding:0; background:{BG}; -webkit-font-smoothing:antialiased; }}
  .wrap {{ max-width:680px; margin:0 auto; padding:32px 20px 48px; }}
  a {{ color:{BLUE}; text-decoration:none; }}
  .tb, .card, td, div {{ font-family:{FONT}; }}
  .k {{ font-size:10px; color:{INK3}; text-transform:uppercase; letter-spacing:.08em; }}
  .k2 {{ font-size:11px; color:{INK3}; text-transform:uppercase; letter-spacing:.08em; }}
  .r {{ border-top:1px solid {LINE}; }}
  .m {{ font-family:{MONO}; }}
  @media only screen and (max-width: 520px) {{
    .wrap {{ padding:20px 12px 40px !important; }}
    .card {{ padding:18px !important; border-radius:14px !important; }}
    .stack {{ display:block !important; width:100% !important; padding:0 0 14px !important; }}
    .cell {{ width:50% !important; }}
    .m, .mono {{ font-size:10px !important; }}
    .hero {{ font-size:30px !important; }}
  }}
</style>
</head>
<body style="margin:0;padding:0;background:{BG}; color:{INK}">
<div class="wrap">

  <div style="padding:8px 0 22px">
    <div style="font-size:11px;color:{INK3};text-transform:uppercase;letter-spacing:.12em">SEC 13F Signal Engine</div>
    <div class="hero" style="font-size:38px;font-weight:700;letter-spacing:-.03em;line-height:1.1;color:{INK};margin-top:8px">Top 10 institutional signals</div>
    <div style="font-size:15px;color:{INK2};margin-top:10px;line-height:1.5">
      {esc(today_str)}{f" · {esc(quarter)}" if quarter else ""} · {a.get('filer_count', '')} filers · {f"{scored_n:,} stocks scored · " if scored_n else ""}{n_ins} of {len(top)} with net insider buying · {opt_line}
    </div>
  </div>

  <div class="card" style="background:{CARD};border:1px solid {LINE};border-radius:18px;padding:18px 24px 8px;overflow-x:auto">
    {summary_table(top)}
  </div>

  {f'<div style="font-size:15px;color:{INK};line-height:1.6;margin:26px 4px 0">{esc(ctx)}</div>' if ctx else ''}

  {section_title("The signals", "Which stocks combine high-conviction institutional accumulation with confirming insider activity, and which Call expresses each")}
  {cards}

  {sell_block(a.get('sell_signals', []))}
  {track_record_block(a.get('backtest'))}
  {methodology_block(a)}

  <div style="font-size:11px;color:{INK3};line-height:1.6;margin-top:28px;padding:0 4px">
    {esc(a.get('disclaimer', ''))} 13F filings show long US equity positions over $200K with up to a 45-day lag; portfolio weights use long-only reported AUM. Form 4 data covers open-market purchases and sales reported to the SEC since the last 13F quarter-end. Generated automatically.
  </div>
</div>
</body>
</html>"""


# ── e-mail ────────────────────────────────────────────────────────────────────

def resolve_recipient(env: dict | None = None) -> str:
    """
    REPORT_RECIPIENT is optional. GitHub Actions still defines the variable for
    an unset secret, as the EMPTY STRING - and os.environ.get() returns that
    empty value instead of the default, which sends the report to "" and gets a
    555 from Gmail. Fall back on any blank/whitespace value, not just a missing key.
    """
    env = os.environ if env is None else env
    return (env.get("REPORT_RECIPIENT") or "").strip() or (env.get("GMAIL_ADDRESS") or "").strip()


def send_gmail(html_content: str, today_str: str) -> None:
    gmail_address  = os.environ.get("GMAIL_ADDRESS", "").strip()
    gmail_password = os.environ.get("GMAIL_APP_PASSWORD", "").strip()
    if not gmail_address or not gmail_password:
        raise ValueError("GMAIL_ADDRESS and GMAIL_APP_PASSWORD must be set")
    recipient = resolve_recipient()
    if "@" not in recipient:
        raise ValueError(
            f"Refusing to send: resolved recipient {recipient!r} is not an e-mail address "
            "(set REPORT_RECIPIENT, or leave it unset to use GMAIL_ADDRESS)"
        )

    msg = MIMEMultipart("alternative")
    msg["Subject"] = REPORT_SUBJECT.format(date=today_str)
    msg["From"]    = gmail_address
    msg["To"]      = recipient
    msg.attach(MIMEText("Your mail client does not render HTML. Open the attached report in a browser.", "plain"))
    msg.attach(MIMEText(html_content, "html"))

    with smtplib.SMTP(GMAIL_SMTP_HOST, GMAIL_SMTP_PORT) as server:
        server.ehlo()
        server.starttls()
        server.login(gmail_address, gmail_password)
        server.sendmail(gmail_address, [recipient], msg.as_string())
    print(f"  ✅ Email sent to {recipient}")


def run(today_str: str | None = None, send: bool = True) -> str:
    today_str = today_str or run_date()
    print(f"\n{'='*60}\nReport – {today_str}\n{'='*60}")

    path = DATA_DIR / f"{today_str}_final_analysis.json"
    if not path.exists():
        raise FileNotFoundError(f"Final analysis not found: {path}")
    analysis = json.load(open(path))
    top = analysis.get("top10", [])
    if not top:
        raise RuntimeError("Report validation failed: empty Top 10 – aborting send")
    for s in top:
        if not (s.get("option") or {}).get("status"):
            raise RuntimeError(f"Report validation failed: {s['ticker']} has no option status")
    print(f"✅ Validation passed: {len(top)} stocks")

    bt_path = DATA_DIR / f"{today_str}_backtest.json"
    if bt_path.exists():
        try:
            analysis["backtest"] = json.load(open(bt_path))
        except (OSError, json.JSONDecodeError):
            pass
    html_out = generate_html_report(analysis)
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    report_path = REPORTS_DIR / f"{today_str}_report.html"
    with open(report_path, "w") as f:
        f.write(html_out)
    print(f"💾 HTML report saved to {report_path} ({len(html_out)//1024} KB)")

    if send:
        send_gmail(html_out, today_str)
    else:
        print("  (email send skipped)")
    return html_out


if __name__ == "__main__":
    import sys
    run(send="--no-send" not in sys.argv)
