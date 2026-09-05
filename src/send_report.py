"""
send_report.py
Generates the HTML report and sends it via Gmail.

Fixes from architecture audit:
  R-14  Validates report is non-empty before sending
  R-06  Report only sends if analysis JSON is structurally complete
"""

import json
import os
import smtplib
from datetime import date
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

from config import (
    DATA_DIR, GMAIL_SMTP_HOST, GMAIL_SMTP_PORT,
    REPORT_SUBJECT, REPORTS_DIR,
)


def load_final_analysis(today_str: str) -> dict:
    path = DATA_DIR / f"{today_str}_final_analysis.json"
    if not path.exists():
        raise FileNotFoundError(f"Final analysis not found: {path}")
    with open(path) as f:
        return json.load(f)


def load_backtest(today_str: str) -> dict | None:
    path = DATA_DIR / f"{today_str}_backtest.json"
    if not path.exists():
        return None
    with open(path) as f:
        return json.load(f)


def flag_badge(flag: str) -> str:
    colors = {
        "HIGH_CONVICTION": "#dc2626",
        "CLUSTER":         "#7c3aed",
        "NEW_POSITION":    "#059669",
        "AGGRESSIVE_ADD":  "#d97706",
        "TOP10_ENTRY":     "#0284c7",
        "EARLY_SMART_MONEY_ACCUMULATION": "#be123c",
    }
    color = colors.get(flag, "#6b7280")
    return f'<span style="background:{color};color:white;padding:2px 8px;border-radius:12px;font-size:11px;font-weight:600;margin-right:4px">{flag}</span>'


def _signal_badge(signal: str) -> str:
    colors = {
        "VERY_STRONG_BUY": ("#fee2e2", "#dc2626", "🔥 VERY STRONG BUY"),
        "STRONG":          ("#ffedd5", "#c2410c", "📈 STRONG"),
        "MODERATE":        ("#fef9c3", "#a16207", "➕ MODERATE"),
        "WEAK":            ("#f1f5f9", "#64748b", "〰️ WEAK"),
        "NEGATIVE":        ("#e5e7eb", "#374151", "🔻 NEGATIVE"),
    }
    bg, color, label = colors.get(signal or "", ("#f1f5f9", "#64748b", signal or "n/a"))
    return (f'<span style="background:{bg};color:{color};padding:4px 12px;border-radius:12px;'
            f'font-size:12px;font-weight:700">{label}</span>')


def _crowding_badge(label: str) -> str:
    colors = {
        "LOW":      ("#f0fdf4", "#166534"),
        "MODERATE": ("#fffbeb", "#92400e"),
        "HIGH":     ("#fff1f2", "#be123c"),
        "EXTREME":  ("#fef2f2", "#991b1b"),
    }
    bg, color = colors.get(label or "", ("#f1f5f9", "#64748b"))
    return (f'<span style="background:{bg};color:{color};padding:2px 10px;border-radius:10px;'
            f'font-size:11px;font-weight:700">CROWDING: {label or "n/a"}</span>')


def _bullet_list(items: list[str], color: str = "#374151") -> str:
    if not items:
        return ""
    lis = "".join(f'<li style="margin-bottom:4px">{i}</li>' for i in items[:5])
    return f'<ul style="margin:4px 0 0 0;padding-left:18px;color:{color};font-size:13px;line-height:1.5">{lis}</ul>'


def _manager_activity_table(rows: list[dict]) -> str:
    """Section 20 table: Manager | Quality | Status | Weight before | Weight now | Shares Δ | Active Weight proxy."""
    if not rows:
        return ""
    trs = ""
    for r in rows:
        weight_before = r.get("weight_before_pct")
        weight_now    = r.get("weight_now_pct")
        shares_chg    = r.get("shares_change_pct")
        wvm           = r.get("weight_vs_median")
        trs += (
            "<tr>"
            f"<td style='padding:5px 8px;color:#111827;font-weight:600'>{r.get('manager','')}</td>"
            f"<td style='padding:5px 8px;color:#6b7280'>{r.get('quality_score','?')}</td>"
            f"<td style='padding:5px 8px;color:#6b7280'>{r.get('status','')}</td>"
            f"<td style='padding:5px 8px;color:#6b7280'>{f'{weight_before:.2f}%' if weight_before is not None else '—'}</td>"
            f"<td style='padding:5px 8px;color:#111827'>{f'{weight_now:.2f}%' if weight_now is not None else '—'}</td>"
            f"<td style='padding:5px 8px;color:#6b7280'>{f'{shares_chg:+.0f}%' if shares_chg is not None else 'NEW'}</td>"
            f"<td style='padding:5px 8px;color:#6b7280'>{f'{wvm:.1f}x median' if wvm is not None else '—'}</td>"
            "</tr>"
        )
    return f"""
    <div style="overflow-x:auto;margin-bottom:12px">
    <table style="width:100%;border-collapse:collapse;font-size:12px;background:#f9fafb;border-radius:8px;">
      <thead><tr style="text-align:left;color:#9ca3af;font-size:10px;text-transform:uppercase">
        <th style="padding:5px 8px">Manager</th><th style="padding:5px 8px">Quality</th>
        <th style="padding:5px 8px">Status</th><th style="padding:5px 8px">Weight before</th>
        <th style="padding:5px 8px">Weight now</th><th style="padding:5px 8px">Shares Δ</th>
        <th style="padding:5px 8px">vs. Manager Median</th>
      </tr></thead>
      <tbody>{trs}</tbody>
    </table>
    </div>"""


def _post_filing_block(perf: dict) -> str:
    """
    Renders a compact coloured bar showing price movement since the 13F filing date.
    Three zones:
      green  : ≤ +15%  → fresh signal, thesis not yet priced in
      amber  : +15–25% → already running, watch closely
      red    : > +25%  → likely priced in / FOMO territory
    """
    pct   = perf.get("pct_change")
    days  = perf.get("days_since_filing")
    fc    = perf.get("filing_close")
    cp    = perf.get("current_price")

    if pct is None:
        return ""

    if pct > 25:
        bg, border, text_color = "#fef2f2", "#fca5a5", "#991b1b"
        label  = f"⚠️ +{pct:.1f}% seit Filing – FOMO-Risiko, Thesis möglicherweise eingepreist"
        icon   = "🔴"
    elif pct > 15:
        bg, border, text_color = "#fffbeb", "#fcd34d", "#92400e"
        label  = f"⚡ +{pct:.1f}% seit Filing – bereits gelaufen, erhöhtes Einstiegsrisiko"
        icon   = "🟡"
    elif pct >= 0:
        bg, border, text_color = "#f0fdf4", "#bbf7d0", "#166534"
        label  = f"✅ +{pct:.1f}% seit Filing – Signal noch frisch"
        icon   = "🟢"
    else:
        bg, border, text_color = "#f0fdf4", "#bbf7d0", "#166534"
        label  = f"↘ {pct:.1f}% seit Filing – günstiger als zum Signal-Zeitpunkt"
        icon   = "🟢"

    days_str  = f"{days}d" if days is not None else "?d"
    price_str = (
        f"${fc} → ${cp}" if fc is not None and cp is not None else ""
    )

    return (
        f'<div style="background:{bg};border:1px solid {border};border-radius:8px;'
        f'padding:10px 14px;margin-bottom:12px;display:flex;'
        f'align-items:center;justify-content:space-between;">'
        f'<span style="color:{text_color};font-size:13px;font-weight:600">{label}</span>'
        f'<span style="color:{text_color};font-size:12px;white-space:nowrap;margin-left:12px">'
        f'{price_str} &nbsp;·&nbsp; {days_str} ago</span>'
        f'</div>'
    )


def _multi_quarter_block(mq: dict) -> str:
    """
    Renders a compact banner when a stock has been built over multiple quarters.
    Only shown when build_quarters >= 2; becomes more prominent at >= 3 and >= 5.
    """
    if not mq:
        return ""
    bq = mq.get("build_quarters", 0)
    if bq < 2:
        return ""

    avg_delta  = mq.get("avg_delta_pct")
    silent     = mq.get("silent_build", False)
    flags      = mq.get("flags", [])

    if bq >= 5:
        bg, border, color, icon = "#fdf4ff", "#d8b4fe", "#6b21a8", "🔥"
    elif bq >= 3:
        bg, border, color, icon = "#eff6ff", "#bfdbfe", "#1e40af", "📈"
    else:
        bg, border, color, icon = "#f0fdf4", "#bbf7d0", "#166534", "➕"

    delta_str = f" · Ø&nbsp;+{avg_delta:.0f}%&nbsp;/&nbsp;Quartal" if avg_delta else ""
    silent_badge = (
        '&nbsp;<span style="background:#7c3aed;color:white;padding:1px 7px;'
        'border-radius:10px;font-size:10px;font-weight:700">SILENT BUILD</span>'
        if silent else ""
    )
    strong_badge = (
        '&nbsp;<span style="background:#dc2626;color:white;padding:1px 7px;'
        'border-radius:10px;font-size:10px;font-weight:700">STRONG BUILD</span>'
        if "STRONG_BUILD" in flags else ""
    )

    return (
        f'<div style="background:{bg};border:1px solid {border};border-radius:8px;'
        f'padding:9px 14px;margin-bottom:12px;">'
        f'<span style="color:{color};font-size:13px;font-weight:600">'
        f'{icon} {bq} Quartale in Folge aufgebaut{delta_str}'
        f'</span>{silent_badge}{strong_badge}'
        f'</div>'
    )


def generate_backtest_html(backtest: dict) -> str:
    """Renders a compact performance summary block for the report."""
    s = backtest.get("summary", {})
    rows = backtest.get("results", [])

    if not s.get("completed_90d"):
        return ""

    wr90  = s.get("win_rate_90d_pct")
    wr180 = s.get("win_rate_180d_pct")
    ar90  = s.get("avg_return_90d_pct")
    ar180 = s.get("avg_return_180d_pct")

    def _color(val):
        if val is None:
            return "#6b7280"
        return "#059669" if val > 0 else "#dc2626"

    def _fmt(val):
        if val is None:
            return "pending"
        return f"+{val:.1f}%" if val > 0 else f"{val:.1f}%"

    # Recent signals table (last 10 completed)
    completed = [r for r in rows if r.get("status_d90") in ("win", "loss")][-10:]
    rows_html = ""
    for r in reversed(completed):
        ret   = r.get("return_d90_pct")
        color = _color(ret)
        rows_html += (
            f"<tr>"
            f"<td style='padding:4px 8px;color:#374151'>{r['report_date']}</td>"
            f"<td style='padding:4px 8px;font-weight:600;color:#111827'>{r['ticker']}</td>"
            f"<td style='padding:4px 8px;color:#6b7280;font-size:12px'>{r.get('primary_flag','')}</td>"
            f"<td style='padding:4px 8px;font-weight:700;color:{color}'>{_fmt(ret)}</td>"
            f"</tr>"
        )

    return f"""
    <div style="background:#f8fafc;border:1px solid #e2e8f0;border-radius:12px;padding:20px;margin-bottom:20px;">
      <div style="font-size:14px;font-weight:700;color:#1e293b;margin-bottom:14px">
        📈 Historical Performance (90-day stock returns)
      </div>
      <div style="display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-bottom:16px;">
        <div style="text-align:center">
          <div style="font-size:22px;font-weight:800;color:{_color(ar90)}">{_fmt(ar90)}</div>
          <div style="font-size:11px;color:#64748b">Avg 90d Return</div>
        </div>
        <div style="text-align:center">
          <div style="font-size:22px;font-weight:800;color:#1e293b">{wr90 if wr90 is not None else '—'}{'%' if wr90 else ''}</div>
          <div style="font-size:11px;color:#64748b">90d Win Rate</div>
        </div>
        <div style="text-align:center">
          <div style="font-size:22px;font-weight:800;color:{_color(ar180)}">{_fmt(ar180)}</div>
          <div style="font-size:11px;color:#64748b">Avg 180d Return</div>
        </div>
        <div style="text-align:center">
          <div style="font-size:22px;font-weight:800;color:#1e293b">{s.get('total_signals','—')}</div>
          <div style="font-size:11px;color:#64748b">Total Signals</div>
        </div>
      </div>
      {'<table style="width:100%;border-collapse:collapse;font-size:13px">' + rows_html + '</table>' if rows_html else ''}
      <div style="font-size:11px;color:#94a3b8;margin-top:10px">
        ⚠️ Past stock returns do not predict option profits. Options can expire worthless even when the stock moves in the right direction.
      </div>
    </div>"""


def _option_trade_block(opt: dict) -> str:
    """Renders the recommended option trade card, including spread legs when applicable."""
    strategy    = opt.get("strategy", "LONG_CALL")
    is_spread   = strategy == "BULL_CALL_SPREAD" or opt.get("short_leg_symbol")
    iv_note     = opt.get("iv_rank_note", "")

    symbol_label = "BUY LEG" if is_spread else "SYMBOL"
    symbol_val   = opt.get("option_symbol", "")
    extra_row    = ""
    if is_spread and opt.get("short_leg_symbol"):
        extra_row = f"""
              <div style="grid-column:1/-1;background:#fef2f2;border-radius:6px;padding:8px 10px;
                          font-size:12px;color:#991b1b;">
                <strong>SELL LEG:</strong>
                <span style="font-family:monospace">{opt.get("short_leg_symbol","")}</span>
                &nbsp; strike ${opt.get("short_strike","?")}
                &nbsp;·&nbsp; Bull Call Spread reduces net premium paid
              </div>"""

    strategy_badge = ""
    if is_spread:
        strategy_badge = (
            '<span style="background:#7c3aed;color:white;padding:2px 8px;border-radius:10px;'
            'font-size:10px;font-weight:700;margin-left:8px">BULL CALL SPREAD</span>'
        )

    iv_note_html = (
        f'<div style="font-size:12px;color:#6b7280;margin-top:6px">'
        f'<strong>IV Note:</strong> {iv_note}</div>'
    ) if iv_note else ""

    return f'''
          <div style="background:#f0fdf4;border:1px solid #bbf7d0;border-radius:8px;padding:16px;">
            <div style="font-size:12px;font-weight:700;color:#166534;text-transform:uppercase;margin-bottom:10px">
              📈 RECOMMENDED OPTION TRADE{strategy_badge}
            </div>
            <div style="display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin-bottom:12px;">
              <div>
                <div style="font-size:10px;color:#6b7280">{symbol_label}</div>
                <div style="font-weight:700;color:#111827;font-family:monospace">{symbol_val}</div>
              </div>
              <div>
                <div style="font-size:10px;color:#6b7280">TYPE / STRIKE</div>
                <div style="font-weight:600;color:#111827">{opt.get("option_type","")} @ ${opt.get("strike","")}</div>
              </div>
              <div>
                <div style="font-size:10px;color:#6b7280">EXPIRY</div>
                <div style="font-weight:600;color:#111827">{opt.get("expiration","")}</div>
              </div>
              <div>
                <div style="font-size:10px;color:#6b7280">NET PREMIUM / CONTRACT</div>
                <div style="font-weight:700;color:#059669">${opt.get("entry_price_mid","?")}</div>
              </div>
              <div>
                <div style="font-size:10px;color:#6b7280">MAX RISK / CONTRACT</div>
                <div style="font-weight:600;color:#dc2626">${opt.get("max_risk_per_contract","?")}</div>
              </div>
              <div>
                <div style="font-size:10px;color:#6b7280">PROFIT TARGET</div>
                <div style="font-weight:600;color:#059669">{opt.get("profit_target","")}</div>
              </div>
              {extra_row}
            </div>
            <div style="font-size:13px;color:#374151;margin-bottom:8px">
              <strong>Rationale:</strong> {opt.get("option_rationale","")}
            </div>
            {iv_note_html}
            <div style="font-size:12px;color:#6b7280;margin-top:6px">
              Stop Loss: {opt.get("stop_loss","")} &nbsp;|&nbsp;
              Greeks: {opt.get("greeks_note","Unknown")}
            </div>
          </div>'''


def generate_sell_signals_html(sell_signals: list[dict]) -> str:
    """Section 14: notable exits/reductions – negative signals, shown separately from the buy-side Top 5."""
    if not sell_signals:
        return ""

    rows_html = ""
    for s in sell_signals[:10]:
        if s["type"] == "EXIT":
            detail = (f"Exited a former rank #{s.get('prior_rank','?')} position"
                      + (" (was TOP-5!)" if s.get("was_top5_position") else ""))
        else:
            detail = f"Reduced {s.get('delta_pct','?')}%"
        parallel = (f" · parallel selling with {', '.join(s['parallel_sellers'])}"
                    if s.get("parallel_selling") else "")
        rows_html += (
            "<tr>"
            f"<td style='padding:5px 8px;font-weight:700;color:#111827'>{s.get('ticker','')}</td>"
            f"<td style='padding:5px 8px;color:#6b7280'>{s.get('filer','')} (q={s.get('manager_quality_score','?')})</td>"
            f"<td style='padding:5px 8px;color:#991b1b'>{s.get('type','')}</td>"
            f"<td style='padding:5px 8px;color:#6b7280;font-size:12px'>{detail}{parallel}</td>"
            "</tr>"
        )

    return f"""
    <div style="background:#fff1f2;border:1px solid #fecdd3;border-radius:12px;padding:20px;margin-bottom:20px;">
      <div style="font-size:14px;font-weight:700;color:#9f1239;margin-bottom:10px">
        📉 Notable Exits &amp; Reductions (Section 14 – negative signals, informational only)
      </div>
      <div style="overflow-x:auto">
      <table style="width:100%;border-collapse:collapse;font-size:13px">
        <thead><tr style="text-align:left;color:#9ca3af;font-size:10px;text-transform:uppercase">
          <th style="padding:5px 8px">Ticker</th><th style="padding:5px 8px">Manager</th>
          <th style="padding:5px 8px">Type</th><th style="padding:5px 8px">Detail</th>
        </tr></thead>
        <tbody>{rows_html}</tbody>
      </table>
      </div>
      <div style="font-size:11px;color:#9f1239;margin-top:8px">
        Not part of the Top 5 buy ideas above – shown for risk context (a quality manager
        exiting a name our Top 5 also holds is worth knowing about).
      </div>
    </div>"""


def generate_html_report(analysis: dict, backtest: dict | None = None) -> str:
    today_str      = analysis["date"]
    top5           = analysis.get("round1_top5", [])
    options_recs   = analysis.get("options_recs", [])
    market_context = analysis.get("market_context", "")
    portfolio_note = analysis.get("portfolio_note", "")
    disclaimer     = analysis.get("disclaimer", "")

    backtest_html     = generate_backtest_html(backtest) if backtest else ""
    sell_signals_html = generate_sell_signals_html(analysis.get("sell_signals", []))

    # Build options recommendations section
    options_html = ""
    options_by_ticker = {r["stock_ticker"]: r for r in options_recs}

    for stock in top5:
        ticker = stock["ticker"]
        opt    = options_by_ticker.get(ticker, {})
        primary_flag = stock.get("primary_flag", "")
        flags_html = flag_badge(primary_flag) if primary_flag else ""
        for extra_flag in ("EARLY_SMART_MONEY_ACCUMULATION",):
            if extra_flag != primary_flag and stock.get("early_smart_money") and extra_flag == "EARLY_SMART_MONEY_ACCUMULATION":
                flags_html += flag_badge(extra_flag)

        # Buyers list – prefer enriched filer_details (port weight + delta) if available
        filer_details = stock.get("filer_details", [])
        if filer_details:
            buyer_items = []
            for fd in filer_details:
                name       = fd.get("filer", "")
                port_pct   = fd.get("port_weight_pct")
                delta_type = fd.get("delta_type", "")
                delta_pct  = fd.get("delta_pct")

                detail_parts = []
                if port_pct is not None:
                    detail_parts.append(f"{port_pct:.2f}% of port")
                if delta_type == "NEW":
                    detail_parts.append("NEW")
                elif delta_pct is not None:
                    detail_parts.append(f"{delta_pct:+.0f}%")

                detail = f" ({', '.join(detail_parts)})" if detail_parts else ""
                buyer_items.append(f"<span style='white-space:nowrap'>{name}{detail}</span>")
            buyers_html = " &nbsp;·&nbsp; ".join(buyer_items)
        else:
            buyers_html = ", ".join(stock.get("key_buyers", []))

        signal_row = _signal_badge(stock.get("signal", "")) if stock.get("signal") else ""
        crowding_row = _crowding_badge(stock.get("crowding_label")) if stock.get("crowding_label") else ""

        narrative_blocks = ""
        for title, key in [
            ("Conviction", "conviction_narrative"),
            ("Accumulation", "accumulation_narrative"),
            ("Smart Money Consensus", "consensus_narrative"),
            ("Institutional Ownership", "institutional_ownership_narrative"),
            ("Freshness", "freshness_narrative"),
        ]:
            text = stock.get(key)
            if text:
                narrative_blocks += (
                    f'<div style="margin-bottom:10px">'
                    f'<div style="font-size:11px;font-weight:700;color:#6b7280;text-transform:uppercase">{title}</div>'
                    f'<div style="font-size:13px;color:#374151;margin-top:2px">{text}</div>'
                    f'</div>'
                )

        why_html   = _bullet_list(stock.get("why_interesting", []), color="#166534")
        risks_html = _bullet_list(stock.get("risks", []), color="#92400e")
        manager_table_html = _manager_activity_table(stock.get("manager_activity", []))
        fazit = stock.get("fazit", "")

        options_html += f"""
        <div style="background:#ffffff;border:1px solid #e5e7eb;border-radius:12px;padding:24px;margin-bottom:20px;">
          <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:12px;">
            <div>
              <span style="font-size:22px;font-weight:700;color:#111827">{ticker}</span>
              <span style="font-size:14px;color:#6b7280;margin-left:8px">{stock.get('company_name','')}</span>
            </div>
            <div style="text-align:right">
              <div style="font-size:28px;font-weight:700;color:#7c3aed">{stock.get('alpha_score', stock.get('conviction_score', ''))}<span style="font-size:14px;color:#9ca3af">/100</span></div>
              <div style="font-size:11px;color:#9ca3af">13F ALPHA SCORE</div>
            </div>
          </div>

          <div style="margin-bottom:12px;display:flex;gap:8px;flex-wrap:wrap;align-items:center">
            {signal_row}{crowding_row}{flags_html}
          </div>

          {_post_filing_block(stock.get("post_filing_perf", {}))}
          {_multi_quarter_block(stock.get("mq_signal", {}))}

          {manager_table_html}

          <div style="background:#f9fafb;border-radius:8px;padding:16px;margin-bottom:16px;">
            <div style="font-size:12px;font-weight:600;color:#6b7280;text-transform:uppercase;margin-bottom:6px">Investment Thesis</div>
            <div style="color:#374151;font-size:14px;line-height:1.6">{stock.get('thesis','')}</div>
          </div>

          {narrative_blocks}

          <div style="display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-bottom:16px;">
            <div style="background:#eff6ff;border-radius:8px;padding:12px;">
              <div style="font-size:11px;color:#3b82f6;font-weight:600">KEY BUYERS</div>
              <div style="color:#1e40af;font-size:13px;margin-top:4px;line-height:1.7">{buyers_html}</div>
            </div>
            <div style="background:#fef3c7;border-radius:8px;padding:12px;">
              <div style="font-size:11px;color:#d97706;font-weight:600">RISK NOTE</div>
              <div style="color:#92400e;font-size:13px;margin-top:4px">{stock.get('risk_factors','')}</div>
            </div>
          </div>

          {f'<div style="margin-bottom:12px"><div style="font-size:11px;font-weight:700;color:#166534;text-transform:uppercase">Why this is interesting</div>{why_html}</div>' if why_html else ""}
          {f'<div style="margin-bottom:16px"><div style="font-size:11px;font-weight:700;color:#92400e;text-transform:uppercase">Risks / possible misreads</div>{risks_html}</div>' if risks_html else ""}

          {f'<div style="background:#f3f4f6;border-radius:8px;padding:14px;margin-bottom:16px;font-size:13px;color:#374151;font-style:italic">{fazit}</div>' if fazit else ""}

          {"" if not opt else _option_trade_block(opt)}
        </div>
        """

    html = f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>SEC 13F Smart Money Report</title>
</head>
<body style="margin:0;padding:0;background:#f3f4f6;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;">

  <div style="max-width:800px;margin:0 auto;padding:20px;">

    <!-- Header -->
    <div style="background:linear-gradient(135deg,#1e1b4b 0%,#312e81 50%,#4f46e5 100%);border-radius:16px;padding:32px;margin-bottom:20px;color:white;">
      <div style="font-size:12px;color:#a5b4fc;text-transform:uppercase;letter-spacing:2px;margin-bottom:8px">
        QUARTERLY INSTITUTIONAL INTELLIGENCE
      </div>
      <div style="font-size:28px;font-weight:800;margin-bottom:4px">SEC 13F Smart Money Report</div>
      <div style="font-size:16px;color:#c7d2fe">{today_str} &nbsp;·&nbsp; 13 Monitored Institutions &nbsp;·&nbsp; Top 5 Picks</div>
    </div>

    <!-- Market Context -->
    <div style="background:#fffbeb;border:1px solid #fcd34d;border-radius:12px;padding:20px;margin-bottom:20px;">
      <div style="font-size:12px;font-weight:700;color:#92400e;text-transform:uppercase;margin-bottom:8px">Market Context</div>
      <div style="color:#78350f;font-size:14px;line-height:1.6">{market_context}</div>
    </div>

    {backtest_html}

    <!-- Top 5 Picks -->
    <div style="font-size:20px;font-weight:700;color:#111827;margin-bottom:16px">
      🎯 Top 5 Conviction Picks + Option Trades
    </div>

    {options_html}

    {sell_signals_html}

    <!-- Portfolio Note -->
    {f'<div style="background:#f0f9ff;border:1px solid #bae6fd;border-radius:12px;padding:20px;margin-bottom:20px;"><div style="font-size:12px;font-weight:700;color:#0369a1;text-transform:uppercase;margin-bottom:8px">Portfolio Sizing Note</div><div style="color:#0c4a6e;font-size:14px">{portfolio_note}</div></div>' if portfolio_note else ''}

    <!-- Disclaimer -->
    <div style="background:#f9fafb;border:1px solid #e5e7eb;border-radius:12px;padding:20px;margin-bottom:20px;">
      <div style="font-size:11px;color:#6b7280;line-height:1.6">
        ⚠️ <strong>DISCLAIMER:</strong> {disclaimer}<br><br>
        <strong>Data limitations:</strong> 13F filings reflect long equity positions over $200K with up to 45-day delay.
        Portfolio weights use long-only reported AUM (cash, shorts, bonds excluded – weights are systematically overstated).
        Stock splits have been adjusted. Option prices are from Tradier at time of analysis and change constantly.
        This report is generated automatically and does not constitute investment advice.
      </div>
    </div>

    <!-- Footer -->
    <div style="text-align:center;font-size:11px;color:#9ca3af;padding:16px;">
      Generated automatically by SEC 13F Smart Money Analyzer &nbsp;·&nbsp;
      Data: SEC EDGAR + Tradier &nbsp;·&nbsp; Analysis: Claude AI
    </div>

  </div>
</body>
</html>"""

    return html


def send_gmail(html_content: str, today_str: str):
    """
    Sends the report via Gmail using App Password (no OAuth required).
    Both GMAIL_ADDRESS and GMAIL_APP_PASSWORD come from GitHub Secrets.
    """
    gmail_address  = os.environ.get("GMAIL_ADDRESS", "")
    gmail_password = os.environ.get("GMAIL_APP_PASSWORD", "")

    if not gmail_address or not gmail_password:
        raise ValueError("GMAIL_ADDRESS and GMAIL_APP_PASSWORD must be set as GitHub Secrets")

    subject = REPORT_SUBJECT.format(date=today_str)

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = gmail_address
    msg["To"]      = gmail_address  # send to yourself

    msg.attach(MIMEText(html_content, "html"))

    with smtplib.SMTP(GMAIL_SMTP_HOST, GMAIL_SMTP_PORT) as server:
        server.ehlo()
        server.starttls()
        server.login(gmail_address, gmail_password)
        server.sendmail(gmail_address, gmail_address, msg.as_string())

    print(f"  ✅ Email sent to {gmail_address}")


def run():
    today_str = date.today().isoformat()

    print(f"\n{'='*60}")
    print(f"Report Generation & Gmail – {today_str}")
    print(f"{'='*60}")

    analysis = load_final_analysis(today_str)

    # R-14 Fix: Validate before sending
    recs = analysis.get("options_recs", [])
    top5 = analysis.get("round1_top5", [])

    if not top5 or not recs:
        raise RuntimeError(
            f"Report validation failed: top5={len(top5)}, options_recs={len(recs)}. "
            "Aborting email send."
        )

    print(f"✅ Validation passed: {len(top5)} stocks, {len(recs)} option recommendations")

    # Generate HTML
    backtest = load_backtest(today_str)
    if backtest:
        print(f"📊 Backtest data loaded ({backtest['summary'].get('total_signals',0)} signals tracked)")
    html = generate_html_report(analysis, backtest)

    # Save report to /reports/
    report_path = REPORTS_DIR / f"{today_str}_report.html"
    with open(report_path, "w") as f:
        f.write(html)
    print(f"💾 HTML report saved to {report_path}")

    # Send email
    print(f"📧 Sending via Gmail...")
    send_gmail(html, today_str)

    print(f"\n🎉 Pipeline complete for {today_str}")


if __name__ == "__main__":
    run()
