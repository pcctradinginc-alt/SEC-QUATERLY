"""
llm_router.py
Cost-aware model routing + cascading for every Claude call in the pipeline.

Design
------
* Routing   – each task name maps to an ordered list of model tiers
              (config.LLM_TASK_ROUTES), cheapest first.
* Cascading – the cheapest tier is tried first; its structured output is run
              through a task-specific validator. Only if validation fails
              (or the API errors out) does the router escalate to the next tier.
              The most capable tier (Opus 5) is the quality anchor at the top.
* Caching   – every request is keyed by sha256(task + system + user + schema).
              A hit returns the stored output with zero API cost, which also
              makes narratives reproducible for identical input data.
* Budget    – a per-run USD budget (config.LLM_MAX_RUN_COST_USD). When it is
              exhausted the router returns None and callers fall back to the
              deterministic rule-based text, so the report always ships.
* Ledger    – token usage and estimated cost per call are appended to
              data/llm_cache/usage_ledger.jsonl for auditing.

All model IDs and prices live in config.LLM_MODELS.
"""

import hashlib
import json
import os
import time
from datetime import datetime, timezone
from typing import Callable

from config import (
    CLAUDE_MAX_TOKENS, CLAUDE_RETRY_COUNT, CLAUDE_RETRY_DELAY, LLM_CACHE_DIR,
    LLM_MAX_RUN_COST_USD, LLM_MODELS, LLM_TASK_ROUTES,
)

Validator = Callable[[dict], tuple[bool, str]]


class RunBudget:
    """Accumulates cost across all router calls in one pipeline run."""

    def __init__(self, max_usd: float = LLM_MAX_RUN_COST_USD):
        self.max_usd = max_usd
        self.spent_usd = 0.0
        self.calls = 0
        self.cache_hits = 0
        self.escalations = 0

    def can_spend(self, estimate: float) -> bool:
        return self.spent_usd + estimate <= self.max_usd

    def summary(self) -> dict:
        return {
            "spent_usd":   round(self.spent_usd, 4),
            "budget_usd":  self.max_usd,
            "api_calls":   self.calls,
            "cache_hits":  self.cache_hits,
            "escalations": self.escalations,
        }


_BUDGET = RunBudget()


def budget() -> RunBudget:
    return _BUDGET


def _cache_key(task: str, system: str, user: str, tool: dict) -> str:
    """Model-agnostic: a validated answer from any tier satisfies the same input."""
    payload = json.dumps(
        {"task": task, "system": system, "user": user, "tool": tool},
        sort_keys=True, ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _estimate_cost(model_key: str, in_tokens: int, out_tokens: int, cached_tokens: int = 0) -> float:
    m = LLM_MODELS[model_key]
    return (
        (in_tokens - cached_tokens) / 1e6 * m["in_per_mtok"]
        + cached_tokens / 1e6 * m["cache_read_per_mtok"]
        + out_tokens / 1e6 * m["out_per_mtok"]
    )


def _ledger(entry: dict) -> None:
    try:
        with open(LLM_CACHE_DIR / "usage_ledger.jsonl", "a") as f:
            f.write(json.dumps(entry, sort_keys=True) + "\n")
    except OSError:
        pass


def _client():
    import anthropic
    return anthropic.Anthropic()




def _call_once(model_key: str, system: str, user: str, tool: dict, max_tokens: int) -> tuple[dict | None, dict]:
    """One API call with forced tool use. Returns (tool_input, usage_dict)."""
    import anthropic

    client = _client()
    model_id = LLM_MODELS[model_key]["id"]
    last_err = ""
    for attempt in range(1, CLAUDE_RETRY_COUNT + 1):
        try:
            resp = client.messages.create(
                model=model_id,
                max_tokens=max_tokens,
                system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
                tools=[tool],
                tool_choice={"type": "tool", "name": tool["name"]},
                messages=[{"role": "user", "content": user}],
            )
            usage = getattr(resp, "usage", None)
            u = {
                "input_tokens":  getattr(usage, "input_tokens", 0) or 0,
                "output_tokens": getattr(usage, "output_tokens", 0) or 0,
                "cache_read":    getattr(usage, "cache_read_input_tokens", 0) or 0,
                "cache_write":   getattr(usage, "cache_creation_input_tokens", 0) or 0,
            }
            if resp.stop_reason == "refusal":
                return None, {**u, "error": "refusal"}
            for block in resp.content:
                if block.type == "tool_use" and block.name == tool["name"]:
                    data = block.input
                    if isinstance(data, str):
                        data = json.loads(data)
                    return data, u
            return None, {**u, "error": "no_tool_use_block"}
        except (TypeError, anthropic.AuthenticationError, anthropic.PermissionDeniedError) as e:
            # No/invalid credentials: the SDK raises TypeError at request time when
            # nothing resolves. Not retryable and not tier-specific.
            print(f"    ⚠️  {model_id}: credentials unavailable ({type(e).__name__})")
            return None, {"input_tokens": 0, "output_tokens": 0, "cache_read": 0, "cache_write": 0,
                          "error": "no_credentials"}
        except anthropic.RateLimitError:
            wait = CLAUDE_RETRY_DELAY * (2 ** (attempt - 1))
            print(f"    ⏳ rate limit on {model_id}; waiting {wait}s ({attempt}/{CLAUDE_RETRY_COUNT})")
            time.sleep(wait)
            last_err = "rate_limit"
        except anthropic.APIStatusError as e:
            last_err = f"api_status_{e.status_code}"
            if e.status_code < 500 and e.status_code != 429:
                print(f"    ⚠️  {model_id} non-retryable error: {e.message}")
                break
            time.sleep(CLAUDE_RETRY_DELAY * attempt)
        except anthropic.APIConnectionError as e:
            last_err = "connection"
            print(f"    ⚠️  connection error: {e}; retrying")
            time.sleep(CLAUDE_RETRY_DELAY * attempt)
    return None, {"input_tokens": 0, "output_tokens": 0, "cache_read": 0, "cache_write": 0, "error": last_err}


def route(
    task: str,
    system: str,
    user: str,
    tool: dict,
    validator: Validator | None = None,
    max_tokens: int = CLAUDE_MAX_TOKENS,
    use_cache: bool = True,
) -> tuple[dict | None, dict]:
    """
    Runs `task` through its cascade. Returns (structured_output | None, meta).
    meta = {model, cached, escalated_from, cost_usd, attempts: [...]}
    """
    tiers = LLM_TASK_ROUTES.get(task, ["haiku", "sonnet", "opus"])
    meta: dict = {"task": task, "model": None, "cached": False, "cost_usd": 0.0, "attempts": []}

    key = _cache_key(task, system, user, tool)
    cache_path = LLM_CACHE_DIR / f"{key}.json"
    if use_cache and cache_path.exists():
        try:
            cached = json.load(open(cache_path))
            data = cached.get("output")
            ok, why = (validator(data) if validator and data is not None else (data is not None, "cached"))
            if ok:
                _BUDGET.cache_hits += 1
                meta.update({"model": cached.get("model"), "cached": True})
                meta["attempts"].append({"tier": cached.get("tier"), "cached": True})
                return data, meta
        except (json.JSONDecodeError, OSError):
            pass

    for i, tier in enumerate(tiers):
        model_id = LLM_MODELS[tier]["id"]

        # Rough pre-check against budget (chars/4 ≈ tokens)
        est_in = (len(system) + len(user)) // 4
        est_cost = _estimate_cost(tier, est_in, max_tokens // 2)
        if not _BUDGET.can_spend(est_cost):
            print(f"    💸 budget exhausted (${_BUDGET.spent_usd:.3f}/{_BUDGET.max_usd}); skipping {task} on {tier}")
            meta["attempts"].append({"tier": tier, "skipped": "budget"})
            break

        print(f"    🤖 {task} → {model_id}" + (f" (escalated from {tiers[i-1]})" if i else ""))
        data, usage = _call_once(tier, system, user, tool, max_tokens)
        cost = _estimate_cost(tier, usage["input_tokens"] + usage.get("cache_read", 0),
                              usage["output_tokens"], usage.get("cache_read", 0))
        _BUDGET.spent_usd += cost
        _BUDGET.calls += 1
        meta["cost_usd"] = round(meta["cost_usd"] + cost, 5)

        ok, why = (False, usage.get("error", "no_output"))
        if data is not None:
            ok, why = validator(data) if validator else (True, "ok")

        attempt = {"tier": tier, "ok": ok, "why": why, "cost_usd": round(cost, 5), **usage}
        meta["attempts"].append(attempt)
        _ledger({"ts": datetime.now(timezone.utc).isoformat(), **attempt, "task": task})

        if usage.get("error") == "no_credentials":
            print(f"    ℹ️  no Anthropic credentials – {task} falls back to rule-based text")
            break

        if ok:
            meta["model"] = model_id
            if i:
                meta["escalated_from"] = tiers[i - 1]
            if use_cache:
                try:
                    with open(cache_path, "w") as f:
                        json.dump({"task": task, "model": model_id, "tier": tier, "output": data}, f, sort_keys=True)
                except OSError:
                    pass
            return data, meta

        _BUDGET.escalations += 1 if i + 1 < len(tiers) else 0
        print(f"    ↪ {tier} failed validation ({why})" + (" – escalating" if i + 1 < len(tiers) else " – no tier left"))

    return None, meta
