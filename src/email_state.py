"""Persistent, fail-closed delivery checkpoint for quarterly SEC report e-mails."""
import json
import os
from datetime import date, datetime, timezone
from pathlib import Path


def state_path() -> Path:
    override = os.environ.get("SEC_NOTIFICATION_STATE_PATH", "").strip()
    return Path(override) if override else Path(__file__).resolve().parents[1] / "data" / "notification_state.json"


def _validated_quarter(value: str) -> str:
    try:
        d = date.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Invalid reporting quarter: {value!r}") from exc
    if (d.month, d.day) not in ((3, 31), (6, 30), (9, 30), (12, 31)):
        raise ValueError(f"Not a quarter-end reporting period: {value!r}")
    return d.isoformat()


def last_sent_quarter(path: Path | None = None) -> str | None:
    path = path or state_path()
    if not path.exists():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict) or "last_emailed_quarter" not in value:
            raise ValueError("missing last_emailed_quarter")
        return _validated_quarter(value["last_emailed_quarter"])
    except (OSError, ValueError, TypeError) as exc:
        raise RuntimeError(f"Cannot trust e-mail checkpoint {path}: {exc}") from exc


def record_sent(quarter: str, run_date: str, path: Path | None = None) -> None:
    """Record only AFTER SMTP has accepted the message. Never mark a --no-send run."""
    quarter = _validated_quarter(quarter)
    date.fromisoformat(run_date)
    path = path or state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "last_emailed_quarter": quarter,
        "report_run_date": run_date,
        "smtp_accepted_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    temporary = path.with_suffix(".tmp")
    try:
        temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)
