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


def flag_badge(flag: str) -> str:
    colors = {
        "HIGH_CONVICTION": "#dc2626",
        "CLUSTER":         "#7c3aed",
        "NEW_POSITION":    "#059669",
        "AGGRESSIVE_ADD":  "#d97706",
        "TOP10_ENTRY":     "#0284c7",
    }
    color = colors.get(flag, "#6b7280")
    return f'<span style="background:{color};color:white;padding:2px 8px;border-radius:12px;font-size:11px;font-weight:600;margin-right:4px">{flag}</span>'


def generate_html_report(analysis: dict) -> str:
    today_str      = analysis["date"]
    top5           = analysis.get("round1_top5", [])
    options_recs   = analysis.get("options_recs", [])
    market_context = analysis.get("market_context", "")
    portfolio_note = analysis.get("portfolio_note", "")
    disclaimer     = analysis.get("disclaimer", "")

    # Build options recommendations section
    options_html = ""
    options_by_ticker = {r["stock_ticker"]: r for r in options_recs}

    for stock in top5:
        ticker = stock["ticker"]
        opt    = options_by_ticker.get(ticker, {})
        primary_flag = stock.get("primary_flag", "")
        flags_html = flag_badge(primary_flag) if primary_flag else ""

        # Buyers list
        buyers = ", ".join(stock.get("key_buyers", []))

        options_html += f"""
        <div style="background:#ffffff;border:1px solid #e5e7eb;border-radius:12px;padding:24px;margin-bottom:20px;">
          <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:12px;">
            <div>
              <span style="font-size:22px;font-weight:700;color:#111827">{ticker}</span>
              <span style="font-size:14px;color:#6b7280;margin-left:8px">{stock.get('company_name','')}</span>
            </div>
            <div style="text-align:right">
              <div style="font-size:28px;font-weight:700;color:#7c3aed">{stock.get('conviction_score', '')}<span style="font-size:14px;color:#9ca3af">/100</span></div>
              <div style="font-size:11px;color:#9ca3af">CONVICTION SCORE</div>
            </div>
          </div>

          <div style="margin-bottom:12px">{flags_html}</div>

          <div style="background:#f9fafb;border-radius:8px;padding:16px;margin-bottom:16px;">
            <div style="font-size:12px;font-weight:600;color:#6b7280;text-transform:uppercase;margin-bottom:6px">Investment Thesis</div>
            <div style="color:#374151;font-size:14px;line-height:1.6">{stock.get('thesis','')}</div>
          </div>

          <div style="display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-bottom:16px;">
            <div style="background:#eff6ff;border-radius:8px;padding:12px;">
              <div style="font-size:11px;color:#3b82f6;font-weight:600">KEY BUYERS</div>
              <div style="color:#1e40af;font-size:13px;margin-top:4px">{buyers}</div>
            </div>
            <div style="background:#fef3c7;border-radius:8px;padding:12px;">
              <div style="font-size:11px;color:#d97706;font-weight:600">RISK NOTE</div>
              <div style="color:#92400e;font-size:13px;margin-top:4px">{stock.get('risk_factors','')}</div>
            </div>
          </div>

          {"" if not opt else f'''
          <div style="background:#f0fdf4;border:1px solid #bbf7d0;border-radius:8px;padding:16px;">
            <div style="font-size:12px;font-weight:700;color:#166534;text-transform:uppercase;margin-bottom:10px">
              📈 RECOMMENDED OPTION TRADE
            </div>
            <div style="display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin-bottom:12px;">
              <div>
                <div style="font-size:10px;color:#6b7280">SYMBOL</div>
                <div style="font-weight:700;color:#111827;font-family:monospace">{opt.get("option_symbol","")}</div>
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
                <div style="font-size:10px;color:#6b7280">MID PRICE</div>
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
            </div>
            <div style="font-size:13px;color:#374151;margin-bottom:8px">
              <strong>Rationale:</strong> {opt.get("option_rationale","")}
            </div>
            <div style="font-size:12px;color:#6b7280">
              Stop Loss: {opt.get("stop_loss","")} &nbsp;|&nbsp;
              Greeks: {opt.get("greeks_note","Unknown")}
            </div>
          </div>'''}
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

    <!-- Top 5 Picks -->
    <div style="font-size:20px;font-weight:700;color:#111827;margin-bottom:16px">
      🎯 Top 5 Conviction Picks + Option Trades
    </div>

    {options_html}

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
    html = generate_html_report(analysis)

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
