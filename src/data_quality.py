"""
data_quality.py
Hard gate between parsing and signal generation.

A quarter-over-quarter comparison can fail silently in ways that still look like
a successful run: a missing prior quarter, a prior file for the same quarter, a
filer join that matches nothing, or share counts that never parsed. Every one of
those produces the same symptom - every position reads as NEW, nothing is ever
REDUCED and no position ever EXITs - and the engine downstream will happily rank
that noise and mail it out.

So the pipeline fails closed. If the comparison cannot be shown to be sound,
scoring, the signal engine, the options lookup and the report do not run.
"""

import json
from datetime import date

from config import DATA_DIR


class DataQualityFailure(RuntimeError):
    """Raised when the quarter-over-quarter comparison cannot be trusted."""


def delta_summary(parsed: dict) -> dict:
    """Counts every delta category plus the matching statistics behind them."""
    counts = {k: 0 for k in ("NEW", "ADDED", "REDUCED", "UNCHANGED", "SOLD")}
    exits = matched = total = 0
    filers_with_prior = filers_without_prior = capped_without_exits = 0

    for filer in parsed.get("filers", {}).values():
        positions = filer.get("positions", [])
        if not positions and filer.get("error"):
            continue
        has_prior = any(p["delta"]["type"] != "NEW" for p in positions) or \
            bool(filer.get("exited_positions"))
        if filer.get("exit_detection_available") is False and filer.get("is_capped"):
            capped_without_exits += 1
        if has_prior:
            filers_with_prior += 1
        else:
            filers_without_prior += 1

        for p in positions:
            t = p["delta"]["type"]
            counts[t] = counts.get(t, 0) + 1
            total += 1
            if t != "NEW":
                matched += 1
        exits += len(filer.get("exited_positions", []))

    return {
        "counts":               counts,
        "exits":                exits,
        "total_positions":      total,
        "matched_positions":    matched,
        "new_ratio":            (counts["NEW"] / total) if total else 0.0,
        "filers_with_prior":    filers_with_prior,
        "filers_without_prior": filers_without_prior,
        "capped_without_exits": capped_without_exits,
    }


def evaluate(parsed: dict, expected_prior: str | None = None) -> dict:
    """
    Returns {passed, failures, warnings, summary, current_period, prior_period}.
    Pure - it reads the parsed structure and decides, without side effects.
    """
    from parse_13f import infer_report_date, previous_quarter_end

    current = parsed.get("period_of_report") or parsed.get("report_date") or ""
    prior   = parsed.get("prior_report_date") or ""
    required = expected_prior or (previous_quarter_end(current) if current else "")

    s = delta_summary(parsed)
    failures: list[str] = []
    warnings: list[str] = []

    if not current:
        failures.append("current reporting period is unknown")
    if not required:
        failures.append("expected prior quarter cannot be derived")
    elif prior != required:
        failures.append(
            f"loaded prior period {prior or 'UNKNOWN'} is not the required {required}")

    if s["total_positions"] == 0:
        failures.append("no positions were parsed")
    elif s["matched_positions"] == 0:
        failures.append("no current position matched the prior quarter")
    elif s["total_positions"] > 100 and s["new_ratio"] > 0.95:
        failures.append(
            f"{s['new_ratio']:.0%} of positions read as NEW - the comparison is degenerate")

    # Zero EXITs is not proof on its own (every filer could be capped), but it is
    # strong corroboration when the delta mix already looks wrong.
    if s["exits"] == 0 and s["total_positions"] > 500:
        msg = "zero EXITs across the whole universe"
        if s["capped_without_exits"] >= max(1, s["filers_with_prior"]):
            warnings.append(msg + " (every comparable filer has a capped book)")
        elif failures:
            failures.append(msg)
        else:
            warnings.append(msg + " - implausible, review the join")

    if s["filers_without_prior"] and s["filers_with_prior"]:
        warnings.append(f"{s['filers_without_prior']} filers had no prior baseline "
                        f"(new to the universe or newly renamed)")

    return {
        "passed":          not failures,
        "failures":        failures,
        "warnings":        warnings,
        "summary":         s,
        "current_period":  current,
        "prior_period":    prior,
        "expected_prior":  required,
        "checked_at":      date.today().isoformat(),
    }


def render(result: dict) -> str:
    s = result["summary"]
    c = s["counts"]
    lines = [
        "",
        "─" * 60,
        "DELTA SUMMARY",
        "─" * 60,
        f"  Current reporting quarter: {result['current_period'] or 'UNKNOWN'}",
        f"  Expected prior quarter:    {result['expected_prior'] or 'UNKNOWN'}",
        f"  Loaded prior period:       {result['prior_period'] or 'UNKNOWN'}",
        "",
        f"  NEW:        {c.get('NEW', 0):>7,}",
        f"  ADDED:      {c.get('ADDED', 0):>7,}",
        f"  REDUCED:    {c.get('REDUCED', 0):>7,}",
        f"  UNCHANGED:  {c.get('UNCHANGED', 0):>7,}",
        f"  EXIT:       {s['exits']:>7,}",
        "",
        f"  Matched current/prior positions: {s['matched_positions']:,} of {s['total_positions']:,}",
        f"  Filers with valid comparisons:   {s['filers_with_prior']}",
        f"  Filers without prior baseline:   {s['filers_without_prior']}",
        f"  Capped filers without EXIT data: {s['capped_without_exits']}",
        "",
    ]
    for w in result["warnings"]:
        lines.append(f"  ⚠️  {w}")
    for f in result["failures"]:
        lines.append(f"  ❌ {f}")
    lines.append("")
    lines.append(f"DATA QUALITY GATE: {'PASS' if result['passed'] else 'FAIL'}")
    lines.append("─" * 60)
    return "\n".join(lines)


def gate(today_str: str, parsed: dict | None = None, strict: bool = True) -> dict:
    """
    Evaluate, print and persist the verdict. Raises DataQualityFailure when the
    comparison cannot be trusted, so no downstream step produces signals.
    """
    if parsed is None:
        path = DATA_DIR / f"{today_str}_holdings_parsed.json"
        if not path.exists():
            raise DataQualityFailure(f"Parsed holdings not found: {path}")
        with open(path) as f:
            parsed = json.load(f)

    result = evaluate(parsed)
    print(render(result))

    out = DATA_DIR / f"{today_str}_data_quality.json"
    try:
        with open(out, "w") as f:
            json.dump(result, f, indent=2, default=str)
        print(f"🧾 Verdict written to {out}")
    except OSError:
        pass

    if strict and not result["passed"]:
        raise DataQualityFailure(
            "Signal generation aborted: " + "; ".join(result["failures"]))
    return result


if __name__ == "__main__":
    import sys
    from config import run_date

    try:
        gate(run_date())
    except DataQualityFailure as e:
        print(f"\n❌ {e}")
        sys.exit(1)
