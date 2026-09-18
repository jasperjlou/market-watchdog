#!/usr/bin/env python3
"""Convert price/volume anomalies into deduplicated internal research triggers.

Price-only signals are capped at L1.  They may ask an information worker to
look for confirming evidence, but they never notify externally or authorize a
broker write.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import median
from typing import Any


APP_DIR = Path(os.environ.get("APP_DIR", "/app"))
AGENT_DIR = APP_DIR / "agent"
STATE_DIR = AGENT_DIR / "state"
TRIGGER_DIR = STATE_DIR / "ai_bus" / "triggers"
SNAPSHOT_PATH = STATE_DIR / "kline_snapshot_latest.json"
POLICY_PATH = APP_DIR / "config" / "scan_policy.yaml"
OUTPUT_PATH = STATE_DIR / "market_signals_latest.json"
DEDUPE_PATH = STATE_DIR / "market_signal_dedupe.json"


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def utc_iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def load_yaml(path: Path) -> dict[str, Any]:
    try:
        import yaml  # type: ignore
    except Exception:
        return {}
    if not path.exists():
        return {}
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    return payload if isinstance(payload, dict) else {}


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def number(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def item_map(snapshot: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        str(item["symbol"]).upper(): item
        for item in snapshot.get("items", [])
        if isinstance(item, dict) and item.get("symbol")
    }


def theme_candidate(name: str, basket: dict[str, Any], items: dict[str, dict[str, Any]]) -> dict[str, Any] | None:
    threshold = float(basket.get("median_move_threshold_pct", 1.0))
    minimum = int(basket.get("minimum_confirming_symbols", 2))
    values = []
    for raw in basket.get("symbols", []):
        symbol = str(raw).upper()
        change = number(items.get(symbol, {}).get("return_1d_pct"))
        if change is not None:
            values.append((symbol, change))
    upward = [(symbol, change) for symbol, change in values if change >= threshold]
    downward = [(symbol, change) for symbol, change in values if change <= -threshold]
    direction = "bullish" if len(upward) >= len(downward) else "bearish"
    confirming = upward if direction == "bullish" else downward
    if len(confirming) < minimum:
        return None
    return {
        "key": f"theme:{name}:{direction}",
        "kind": "theme_resonance",
        "theme": name,
        "direction": direction,
        "symbols": [symbol for symbol, _ in confirming],
        "median_move_pct": round(median(change for _, change in confirming), 4),
        "confirming_symbols": len(confirming),
        "candidate_level": "L1",
        "requires_confirmation_for_L2": True,
    }


def single_candidates(items: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    result = []
    for symbol, item in items.items():
        flags = {str(value) for value in item.get("flags", [])}
        if not flags & {"intraday_move", "volume_spike", "breakout_20d", "breakdown_20d", "strong_move_5d"}:
            continue
        change = number(item.get("return_1d_pct"))
        direction = "bullish" if change is not None and change >= 0 else "bearish"
        result.append({
            "key": f"symbol:{symbol}:{direction}",
            "kind": "single_symbol_anomaly",
            "direction": direction,
            "symbols": [symbol],
            "return_1d_pct": change,
            "flags": sorted(flags),
            "candidate_level": "L1",
            "requires_confirmation_for_L2": True,
        })
    return result


def analyze(snapshot: dict[str, Any], policy: dict[str, Any]) -> list[dict[str, Any]]:
    items = item_map(snapshot)
    candidates = single_candidates(items)
    baskets = policy.get("theme_baskets", {}) if isinstance(policy, dict) else {}
    for name, basket in baskets.items() if isinstance(baskets, dict) else []:
        if isinstance(basket, dict):
            candidate = theme_candidate(str(name), basket, items)
            if candidate:
                candidates.append(candidate)
    return candidates


def parse_time(value: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(timezone.utc)
    except Exception:
        return None


FINGERPRINT_FIELDS = (
    "symbol", "provider_update_time", "last_close", "return_1d_pct",
    "return_5d_pct", "return_20d_pct", "volume_z_20", "bars", "flags",
)


def snapshot_fingerprint(snapshot: dict[str, Any]) -> str:
    """Fingerprint provider values while ignoring collection-only timestamps."""
    material = []
    for item in snapshot.get("items", []) if isinstance(snapshot, dict) else []:
        if not isinstance(item, dict) or not item.get("symbol"):
            continue
        normalized = {field: item.get(field) for field in FINGERPRINT_FIELDS}
        normalized["symbol"] = str(normalized["symbol"]).upper()
        normalized["flags"] = sorted(str(value) for value in (normalized.get("flags") or []))
        material.append(normalized)
    material.sort(key=lambda item: item["symbol"])
    encoded = json.dumps(material, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def candidate_fingerprint(candidates: list[dict[str, Any]]) -> str:
    """Fingerprint meaningful anomaly membership, not provider recalculation noise."""
    material = []
    for candidate in candidates:
        if not isinstance(candidate, dict) or not candidate.get("key"):
            continue
        material.append({
            "key": str(candidate.get("key")),
            "kind": str(candidate.get("kind") or ""),
            "direction": str(candidate.get("direction") or ""),
            "symbols": sorted(str(value).upper() for value in (candidate.get("symbols") or [])),
            "flags": sorted(str(value) for value in (candidate.get("flags") or [])),
            "candidate_level": str(candidate.get("candidate_level") or "L1"),
        })
    material.sort(key=lambda item: item["key"])
    encoded = json.dumps(material, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def due_candidates(candidates: list[dict[str, Any]], state: dict[str, Any], now: datetime, cooldown_minutes: int) -> list[dict[str, Any]]:
    history = state.get("history", {}) if isinstance(state, dict) else {}
    due = []
    for candidate in candidates:
        last = parse_time(history.get(candidate["key"])) if isinstance(history, dict) else None
        if last is None or now - last >= timedelta(minutes=cooldown_minutes):
            due.append(candidate)
    return due


def priority_score(candidate: dict[str, Any]) -> float:
    if candidate.get("kind") == "theme_resonance":
        return 100.0 + float(candidate.get("confirming_symbols") or 0) * 5.0 + abs(float(candidate.get("median_move_pct") or 0))
    return len(candidate.get("flags") or []) * 10.0 + abs(float(candidate.get("return_1d_pct") or 0))


def select_for_enqueue(
    candidates: list[dict[str, Any]],
    state: dict[str, Any],
    snapshot: dict[str, Any],
    now: datetime,
    *,
    cooldown_minutes: int,
    max_per_cycle: int,
    max_per_hour: int,
) -> dict[str, Any]:
    fingerprint = candidate_fingerprint(candidates)
    previous_fingerprint = str(state.get("last_candidate_fingerprint") or "") if isinstance(state, dict) else ""
    history = state.get("history") if isinstance(state, dict) else {}
    # Migration guard: an existing dedupe history proves the current snapshot was
    # already handled by the old engine.  Record its fingerprint without launching
    # one last burst during the upgrade.
    migrated_existing_state = not previous_fingerprint and bool(history)
    snapshot_changed = not migrated_existing_state and fingerprint != previous_fingerprint
    due = due_candidates(candidates, state, now, cooldown_minutes) if snapshot_changed else []
    recent_times = []
    for value in state.get("recent_trigger_times", []) if isinstance(state, dict) else []:
        parsed = parse_time(value)
        if parsed is not None and timedelta(0) <= now - parsed < timedelta(hours=1):
            recent_times.append(utc_iso(parsed))
    hourly_budget_remaining = max(0, max_per_hour - len(recent_times))
    selected_limit = min(max(0, max_per_cycle), hourly_budget_remaining)
    selected = sorted(due, key=priority_score, reverse=True)[:selected_limit]
    return {
        "selected": selected,
        "due": due,
        "snapshot_fingerprint": fingerprint,
        "snapshot_changed": snapshot_changed,
        "migrated_existing_state": migrated_existing_state,
        "recent_trigger_times": recent_times,
        "hourly_budget_remaining": hourly_budget_remaining,
        "unchanged_snapshot_suppressed_count": len(candidates) if not snapshot_changed else 0,
    }


def enqueue(candidate: dict[str, Any], now: datetime) -> str:
    TRIGGER_DIR.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256((candidate["key"] + utc_iso(now)[:16]).encode("utf-8")).hexdigest()[:16]
    path = TRIGGER_DIR / f"market_anomaly_{digest}.json"
    symbols = ",".join(candidate.get("symbols") or [])
    payload = {
        "version": "1.0",
        "created_at": utc_iso(now),
        "trigger": "price_volume_anomaly",
        "symbols": symbols,
        "execute_workers": False,
        "task": (
            f"Investigate the L1 price-only anomaly {candidate['key']} for {symbols}. "
            "Find fresh official or high-trust evidence, contradictions, and related symbols. "
            "Do not promote to L2 without independent news or official confirmation. "
            "Do not send externally and do not perform broker writes."
        ),
        "price_only_level_cap": "L1",
        "actual_broker_writes": False,
    }
    write_json(path, payload)
    return str(path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--enqueue", action="store_true")
    args = parser.parse_args()
    now = utc_now()
    snapshot = load_json(SNAPSHOT_PATH, {})
    policy = load_yaml(POLICY_PATH)
    candidates = analyze(snapshot, policy)
    state = load_json(DEDUPE_PATH, {"history": {}})
    escalation = policy.get("anomaly_rules", {}).get("escalation", {})
    cooldown = int(escalation.get("anomaly_worker_cooldown_minutes", 60))
    max_queued = max(0, int(escalation.get("max_worker_triggers_per_cycle", 4)))
    max_hourly = max(0, int(escalation.get("max_worker_triggers_per_hour", 4)))
    selection = select_for_enqueue(
        candidates, state, snapshot, now,
        cooldown_minutes=cooldown,
        max_per_cycle=max_queued,
        max_per_hour=max_hourly,
    )
    due = selection["due"]
    selected_due = selection["selected"]
    queued = []
    if args.enqueue:
        history = dict(state.get("history") or {}) if isinstance(state, dict) else {}
        recent_trigger_times = list(selection["recent_trigger_times"])
        for candidate in selected_due:
            queued.append(enqueue(candidate, now))
            history[candidate["key"]] = utc_iso(now)
            recent_trigger_times.append(utc_iso(now))
        write_json(DEDUPE_PATH, {
            "version": "1.1",
            "updated_at": utc_iso(now),
            "history": history,
            "last_candidate_fingerprint": selection["snapshot_fingerprint"],
            "last_snapshot_fingerprint": snapshot_fingerprint(snapshot),
            "recent_trigger_times": recent_trigger_times,
        })
    payload = {
        "version": "1.0",
        "analyzed_at": utc_iso(now),
        "candidate_count": len(candidates),
        "due_count": len(due),
        "selected_due_count": len(selected_due),
        "queued_count": len(queued),
        "max_worker_triggers_per_cycle": max_queued,
        "max_worker_triggers_per_hour": max_hourly,
        "hourly_budget_remaining_before_run": selection["hourly_budget_remaining"],
        "snapshot_changed": selection["snapshot_changed"],
        "candidate_set_changed": selection["snapshot_changed"],
        "unchanged_snapshot_suppressed_count": selection["unchanged_snapshot_suppressed_count"],
        "candidates": candidates,
        "queued": queued,
        "price_only_level_cap": "L1",
        "external_send": False,
        "actual_broker_writes": False,
    }
    write_json(OUTPUT_PATH, payload)
    print(f"MARKET_SIGNAL_ENGINE candidates={len(candidates)} due={len(due)} queued={len(queued)} price_only_cap=L1")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
