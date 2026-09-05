"""
run_pipeline.py
One-command orchestrator for the SEC 13F signal engine.

    python src/run_pipeline.py                 # full run for today
    python src/run_pipeline.py --from scoring  # resume from a step
    python src/run_pipeline.py --no-email      # build the HTML, don't send
    python src/run_pipeline.py --skip-insider  # offline-ish dry run
    python src/run_pipeline.py --date 2026-09-05

Steps (each one also runs standalone):
    fetch      fetch_filings.py    SEC EDGAR 13F XML + CUSIP→ticker
    parse      parse_13f.py        deltas vs. prior quarter
    scoring    scoring.py          per-filer alpha components, crowding, sell signals
    signals    signal_engine.py    deterministic 0-100 SIGNAL SCORE, Form 4, Top-10
    options    options_lookup.py   Tradier chains → one Call per stock or NO_SUITABLE_OPTION_FOUND
    explain    explain_signals.py  cost-routed commentary (Haiku → Sonnet → Opus cascade)
    backtest   backtest.py         historical performance of past Top-10s
    report     send_report.py      HTML e-mail
"""

import argparse
import os
import sys
import time
from datetime import date

STEPS = ["fetch", "parse", "scoring", "signals", "options", "explain", "backtest", "report"]


def main() -> int:
    ap = argparse.ArgumentParser(description="SEC 13F signal engine")
    ap.add_argument("--date", default=date.today().isoformat())
    ap.add_argument("--from", dest="start", choices=STEPS, default="fetch")
    ap.add_argument("--to", dest="stop", choices=STEPS, default="report")
    ap.add_argument("--no-email", action="store_true")
    ap.add_argument("--skip-insider", action="store_true")
    ap.add_argument("--skip-options", action="store_true", help="no Tradier key: mark every stock NOT_EVALUATED")
    args = ap.parse_args()

    if args.date != date.today().isoformat():
        # Every module keys its files on date.today(); allow an override via env
        os.environ["SEC_RUN_DATE"] = args.date

    todo = STEPS[STEPS.index(args.start): STEPS.index(args.stop) + 1]
    print(f"▶ pipeline {args.date}: {' → '.join(todo)}")
    t0 = time.time()

    for step in todo:
        ts = time.time()
        if step == "fetch":
            import fetch_filings; fetch_filings.run()
        elif step == "parse":
            import parse_13f; parse_13f.run()
        elif step == "scoring":
            import scoring; scoring.run()
        elif step == "signals":
            import signal_engine; signal_engine.run(args.date, skip_insider=args.skip_insider)
        elif step == "options":
            if args.skip_options or not os.environ.get("TRADIER_API_KEY"):
                print("\n(options step skipped – no TRADIER_API_KEY)")
            else:
                import options_lookup; options_lookup.run(args.date)
        elif step == "explain":
            import explain_signals; explain_signals.run(args.date)
        elif step == "backtest":
            import backtest; backtest.run()
        elif step == "report":
            import send_report; send_report.run(args.date, send=not args.no_email)
        print(f"   ⏱ {step} {time.time() - ts:.1f}s")

    print(f"\n🎉 done in {time.time() - t0:.0f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
