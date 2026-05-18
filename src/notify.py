"""
notify.py
Sends pipeline failure alerts via Gmail.

Called from GitHub Actions on workflow failure so silent crashes
don't go unnoticed. Uses the same GMAIL_ADDRESS / GMAIL_APP_PASSWORD
secrets already configured for the report.
"""

import os
import smtplib
import sys
import traceback
from datetime import date
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from config import GMAIL_SMTP_HOST, GMAIL_SMTP_PORT


def send_alert(subject: str, body: str):
    """
    Sends a plain-text alert email to GMAIL_ADDRESS.
    Silently skips if credentials are not set (local dev).
    """
    gmail_address  = os.environ.get("GMAIL_ADDRESS", "")
    gmail_password = os.environ.get("GMAIL_APP_PASSWORD", "")

    if not gmail_address or not gmail_password:
        print(f"[notify] No Gmail credentials set – alert not sent.\n{subject}")
        return

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = gmail_address
    msg["To"]      = gmail_address

    html_body = f"""
    <div style="font-family:monospace;background:#fef2f2;border:1px solid #fca5a5;
                border-radius:8px;padding:20px;max-width:700px;">
      <h2 style="color:#dc2626;margin-top:0">⚠️ SEC Analyzer Pipeline Failure</h2>
      <pre style="background:#fff;padding:12px;border-radius:4px;
                  overflow-x:auto;font-size:12px;color:#374151">{body}</pre>
      <p style="color:#6b7280;font-size:12px;margin-bottom:0">
        Date: {date.today().isoformat()} &nbsp;·&nbsp; Check GitHub Actions for full logs.
      </p>
    </div>
    """

    msg.attach(MIMEText(html_body, "html"))

    try:
        with smtplib.SMTP(GMAIL_SMTP_HOST, GMAIL_SMTP_PORT) as server:
            server.ehlo()
            server.starttls()
            server.login(gmail_address, gmail_password)
            server.sendmail(gmail_address, gmail_address, msg.as_string())
        print(f"[notify] Alert sent: {subject}")
    except Exception as e:
        # Don't let the notifier itself crash the workflow
        print(f"[notify] Failed to send alert email: {e}")


if __name__ == "__main__":
    # Called by GitHub Actions on failure:
    #   python src/notify.py "Step Name" "optional extra message"
    step = sys.argv[1] if len(sys.argv) > 1 else "Unknown step"
    extra = sys.argv[2] if len(sys.argv) > 2 else ""

    send_alert(
        subject=f"🚨 SEC Analyzer Pipeline Failed – {step} ({date.today().isoformat()})",
        body=f"Failed step: {step}\n\n{extra}\n\nCheck GitHub Actions for the full log.",
    )
