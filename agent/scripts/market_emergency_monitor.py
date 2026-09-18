#!/usr/bin/env python3
"""Detect confirmed severe moves and portfolio review thresholds.

This monitor may queue an L2 risk warning for strong price resonance, but it
does not claim a cause until news is verified and it never writes to a broker.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable


APP_DIR = Path(os.environ.get("APP_DIR", "/app"))
AGENT_DIR = Path(os.environ.get("AGENT_DIR", str(APP_DIR / "agent")))
STATE_DIR = AGENT_DIR / "state"
SNAPSHOT_PATH = STATE_DIR / "kline_snapshot_latest.json"
PORTFOLIO_PATH = STATE_DIR / "moomoo_portfolio_snapshot_latest.json"
POLICY_PATH = APP_DIR / "config" / "trend_outlook_policy.yaml"
STATE_PATH = STATE_DIR / "market_emergency_state.json"
OUTPUT_PATH = STATE_DIR / "market_emergency_latest.json"
TRIGGER_DIR = STATE_DIR / "ai_bus" / "triggers"
GATEWAY_PATH = AGENT_DIR / "scripts" / "communication_gateway.py"


def utc_iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def load_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return default


def load_yaml(path: Path) -> dict[str, Any]:
    try:
        import yaml  # type: ignore
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {}
    except Exception:
        return {}


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def number(value: Any) -> float | None:
    try:
        parsed = float(value)
        return parsed if math.isfinite(parsed) else None
    except (TypeError, ValueError):
        return None


def parse_time(value: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(timezone.utc)
    except Exception:
        return None


def item_map(snapshot: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        str(item.get("symbol") or "").upper(): item
        for item in (snapshot.get("items") or [])
        if isinstance(item, dict) and item.get("symbol")
    }


def holding_map(portfolio: dict[str, Any]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for item in portfolio.get("positions", []) if isinstance(portfolio, dict) else []:
        if not isinstance(item, dict):
            continue
        contract = item.get("contract") if isinstance(item.get("contract"), dict) else {}
        symbol = str(item.get("symbol") or contract.get("symbol") or "").strip().upper()
        if symbol:
            result[symbol] = item
    return result


def due(last_value: Any, now: datetime, minutes: int) -> bool:
    previous = parse_time(last_value)
    return previous is None or now - previous >= timedelta(minutes=minutes)


def analyze(
    snapshot: dict[str, Any], portfolio: dict[str, Any], policy: dict[str, Any], state: dict[str, Any],
    *, now: datetime,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    emergency = policy.get("emergency") if isinstance(policy.get("emergency"), dict) else {}
    risk_policy = policy.get("risk") if isinstance(policy.get("risk"), dict) else {}
    dedupe_minutes = int(emergency.get("dedupe_minutes", 30))
    confirmation_window = int(emergency.get("confirmation_window_minutes", 20))
    observations = dict(state.get("observations") or {})
    dedupe = dict(state.get("dedupe") or {})
    portfolio_bands = dict(state.get("portfolio_bands") or {})
    alerts: list[dict[str, Any]] = []
    prices = item_map(snapshot)

    for symbol, item in prices.items():
        move = number(item.get("return_1d_pct"))
        if move is None:
            continue
        is_core = str(item.get("scan_tier") or "") == "core"
        bars = int(number(item.get("bars")) or 0)
        new_listing = bars < 20
        overrides = emergency.get("symbol_threshold_overrides") if isinstance(emergency.get("symbol_threshold_overrides"), dict) else {}
        symbol_override = overrides.get(symbol) if isinstance(overrides.get(symbol), dict) else {}
        default_immediate = emergency.get("core_or_etf_immediate_move_pct" if is_core else "single_stock_immediate_move_pct", 3.0 if is_core else 8.0)
        default_confirmed = emergency.get("core_or_etf_confirmed_move_pct" if is_core else "single_stock_confirmed_move_pct", 2.0 if is_core else 5.0)
        immediate_threshold = float(
            emergency.get("new_listing_immediate_move_pct", 15.0)
            if new_listing else symbol_override.get("immediate_move_pct", default_immediate)
        )
        confirmed_threshold = float(symbol_override.get("confirmed_move_pct", default_confirmed))
        direction = "up" if move >= 0 else "down"
        previous = observations.get(symbol) if isinstance(observations.get(symbol), dict) else {}
        previous_at = parse_time(previous.get("observed_at"))
        same_recent = bool(previous_at and now - previous_at <= timedelta(minutes=confirmation_window) and previous.get("direction") == direction)
        observation_marker = str(item.get("provider_update_time") or item.get("collected_at") or snapshot.get("run_at") or "")
        same_snapshot = bool(observation_marker and observation_marker == str(previous.get("observation_marker") or ""))
        if same_snapshot:
            count = int(previous.get("count") or 1)
        else:
            count = int(previous.get("count") or 0) + 1 if same_recent else 1
        observations[symbol] = {
            "direction": direction,
            "count": count,
            "move_pct": move,
            "observed_at": utc_iso(now),
            "observation_marker": observation_marker,
        }
        flags = {str(value) for value in (item.get("flags") or [])}
        immediate = abs(move) >= immediate_threshold
        confirmed = (not new_listing) and abs(move) >= confirmed_threshold and count >= 2
        resonance = bool(item.get("price_resonance")) or bool(flags & {"volume_spike", "breakout_20d", "breakdown_20d", "intraday_move"})
        strong_resonance = immediate or (confirmed and resonance)
        if not strong_resonance:
            continue
        key = f"price:{symbol}:{direction}"
        alert_kind = "new_listing_extreme" if new_listing else ("immediate_extreme" if immediate else "confirmed_severe_move")
        alerts.append({
            "key": key,
            "symbol": symbol,
            "kind": alert_kind,
            "direction": direction,
            "move_pct": round(move, 3),
            "flags": sorted(flags),
            "bars": bars,
            "alert_level": "L2",
            "cause_status": "unconfirmed_pending_news",
            "action": "立即复核持仓、保护位与相关新闻；先不自动交易",
            "priority": "urgent" if immediate else "high",
        })

    profit_threshold = float(risk_policy.get("portfolio_profit_review_pct", 15.0))
    loss_threshold = float(risk_policy.get("portfolio_loss_review_pct", -8.0))
    for symbol, holding in holding_map(portfolio).items():
        price = number(prices.get(symbol, {}).get("last_close"))
        avg_cost = number(holding.get("average_cost") if "average_cost" in holding else holding.get("avg_cost"))
        if not price or not avg_cost:
            continue
        unrealized = (price / avg_cost - 1.0) * 100.0
        band = "profit_review" if unrealized >= profit_threshold else ("loss_review" if unrealized <= loss_threshold else "normal")
        old_band = str(portfolio_bands.get(symbol) or "normal")
        portfolio_bands[symbol] = band
        if band == "normal" or band == old_band:
            continue
        key = f"portfolio:{symbol}:{band}"
        if not due(dedupe.get(key), now, dedupe_minutes):
            continue
        alerts.append({
            "key": key,
            "symbol": symbol,
            "kind": band,
            "direction": "up" if band == "profit_review" else "down",
            "move_pct": round(unrealized, 2),
            "flags": ["portfolio_cost_threshold_crossed"],
            "alert_level": "L2",
            "cause_status": "portfolio_threshold",
            "action": "复核止盈保护" if band == "profit_review" else "复核止损与仓位风险",
            "priority": "high",
        })

    next_state = {
        "version": "2.0",
        "updated_at": utc_iso(now),
        "observations": observations,
        "dedupe": dedupe,
        "portfolio_bands": portfolio_bands,
        "warning_chamber": dict(state.get("warning_chamber") or {}),
        "actual_broker_writes": False,
    }
    return alerts, next_state


def alert_message(alert: dict[str, Any]) -> tuple[str, str]:
    symbol = str(alert.get("symbol") or "?")
    move = alert.get("move_pct")
    subject = f"[market-watchdog] L2 紧急复核 {symbol}"
    body = "\n".join([
        f"相关事件说明：{symbol}出现需要复核的强价格共振，相关消息仍在核实。",
        f"价格趋势：当前变化{move}%，已进入L2警戒仓。",
        f"操作建议：{alert.get('action') or '先控制风险并等待复核'}。",
    ])[:220]
    return subject, body


def enqueue_via_gateway(subject: str, body: str, priority: str) -> dict[str, Any]:
    command = [
        sys.executable, str(GATEWAY_PATH), "--enqueue", "--channel", "telegram",
        "--kind", "market_review_L2", "--priority", priority,
        "--subject", subject, "--body", body,
    ]
    try:
        proc = subprocess.run(command, cwd=str(APP_DIR), text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=30, check=False)
        return {"ok": proc.returncode == 0, "returncode": proc.returncode, "output_tail": proc.stdout[-500:]}
    except Exception as exc:
        return {"ok": False, "returncode": None, "error": exc.__class__.__name__}


def write_research_trigger(
    alert: dict[str, Any], trigger_dir: Path, now: datetime, correlation_id: str, revision: int,
) -> str:
    trigger_dir.mkdir(parents=True, exist_ok=True)
    symbol = str(alert.get("symbol") or "")
    path = trigger_dir / f"{correlation_id}.json"
    payload = {
        "version": "1.0",
        "created_at": utc_iso(now),
        "trigger": "price_volume_anomaly",
        "symbols": symbol,
        "execute_workers": True,
        "correlation_id": correlation_id,
        "warning_revision": revision,
        "workflow": "warning_review",
        "task": (
            f"Urgently investigate {symbol} after {alert.get('kind')} ({alert.get('move_pct')}%). "
            "Search official filings/issuer releases and high-trust news from the last 24 hours, "
            "state bullish/bearish/neutral direction and impact horizon, include URLs, and identify contradictions. "
            "This is evidence collection only: no external sends and no broker writes."
        ),
        "price_risk_warning_already_queued": False,
        "external_send_before_review": False,
        "actual_broker_writes": False,
    }
    write_json(path, payload)
    return str(path)


def stage_warning(
    alert: dict[str, Any], state: dict[str, Any], trigger_dir: Path, now: datetime,
    policy: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Put a candidate in the warning chamber without sending externally."""
    chamber = state.setdefault("warning_chamber", {})
    symbol = str(alert.get("symbol") or "").upper()
    previous = chamber.get(symbol) if isinstance(chamber.get(symbol), dict) else {}
    same_direction = previous.get("direction") == alert.get("direction")
    pending = previous.get("status") == "research_pending" and same_direction
    delta = abs(float(alert.get("move_pct") or 0)) - abs(float(previous.get("last_reviewed_move_pct") or previous.get("move_pct") or 0))
    emergency = policy.get("emergency") if isinstance(policy, dict) and isinstance(policy.get("emergency"), dict) else {}
    renotify_delta = float(emergency.get("renotify_move_delta_pct", 5.0))
    material_worsening = previous.get("status") == "active_warning" and same_direction and delta >= renotify_delta
    requires_review = not previous or not same_direction or previous.get("status") in {"resolved", "expired"} or material_worsening
    if pending:
        requires_review = False

    revision = int(previous.get("revision") or 0) + (1 if requires_review else 0)
    if not previous:
        revision = 1
    correlation_id = str(previous.get("correlation_id") or "")
    trigger_path = None
    status = str(previous.get("status") or "research_pending")
    if requires_review:
        digest = hashlib.sha256(f"{symbol}|{revision}|{alert.get('key')}".encode("utf-8")).hexdigest()[:12]
        correlation_id = f"warning_{symbol.lower().replace('^', 'idx_')}_{revision}_{digest}"
        status = "research_pending"
        trigger_path = write_research_trigger(alert, trigger_dir, now, correlation_id, revision)

    chamber[symbol] = {
        **previous,
        "symbol": symbol,
        "status": status,
        "revision": revision,
        "correlation_id": correlation_id,
        "alert_level": str(alert.get("alert_level") or "L2"),
        "direction": str(alert.get("direction") or "unknown"),
        "move_pct": alert.get("move_pct"),
        "worst_move_pct": max(abs(float(previous.get("worst_move_pct") or 0)), abs(float(alert.get("move_pct") or 0))),
        "kind": str(alert.get("kind") or "market_anomaly"),
        "priority": str(alert.get("priority") or "high"),
        "cause_status": str(alert.get("cause_status") or "unconfirmed_pending_news"),
        "created_at": previous.get("created_at") or utc_iso(now),
        "updated_at": utc_iso(now),
        "pending_reason": "direction_flip" if previous and not same_direction else ("material_worsening" if material_worsening else "initial_L2"),
        "notification_count": int(previous.get("notification_count") or 0),
        "actual_broker_writes": False,
    }
    return {"symbol": symbol, "correlation_id": correlation_id, "trigger_path": trigger_path, "research_queued": bool(trigger_path)}


def update_warning_resolutions(
    snapshot: dict[str, Any], policy: dict[str, Any], state: dict[str, Any], *, now: datetime,
) -> int:
    """Require two distinct calm observations before closing an active warning."""
    emergency = policy.get("emergency") if isinstance(policy.get("emergency"), dict) else {}
    calm_threshold = float(emergency.get("resolution_move_pct", 1.5))
    prices = item_map(snapshot)
    chamber = state.get("warning_chamber") if isinstance(state.get("warning_chamber"), dict) else {}
    ready = 0
    for symbol, warning in chamber.items():
        if not isinstance(warning, dict) or warning.get("status") != "active_warning":
            continue
        item = prices.get(str(symbol).upper(), {})
        move = number(item.get("return_1d_pct"))
        marker = str(item.get("provider_update_time") or item.get("collected_at") or snapshot.get("run_at") or "")
        if move is None or not marker:
            continue
        if abs(move) > calm_threshold:
            warning["resolution_observation_count"] = 0
            warning["resolution_observation_marker"] = marker
            continue
        if marker != str(warning.get("resolution_observation_marker") or ""):
            warning["resolution_observation_count"] = int(warning.get("resolution_observation_count") or 0) + 1
            warning["resolution_observation_marker"] = marker
        warning["move_pct"] = move
        warning["updated_at"] = utc_iso(now)
        if int(warning.get("resolution_observation_count") or 0) >= 2:
            warning["status"] = "resolution_pending"
            warning["revision"] = int(warning.get("revision") or 0) + 1
            warning["resolution_reason"] = "two_distinct_calm_snapshots"
            ready += 1
        chamber[symbol] = warning
    state["warning_chamber"] = chamber
    return ready


def run_monitor(
    snapshot_path: Path, portfolio_path: Path, policy_path: Path, state_path: Path,
    *, enqueue: bool, now: datetime | None = None,
    sender: Callable[[str, str, str], dict[str, Any]] = enqueue_via_gateway,
    trigger_dir: Path = TRIGGER_DIR,
) -> dict[str, Any]:
    current = now or datetime.now(timezone.utc)
    snapshot = load_json(snapshot_path, {"items": []})
    portfolio = load_json(portfolio_path, {"positions": []})
    policy = load_yaml(policy_path)
    alerts, next_state = analyze(
        snapshot,
        portfolio,
        policy,
        load_json(state_path, {}),
        now=current,
    )
    results = []
    if enqueue:
        for alert in alerts:
            staged = stage_warning(alert, next_state, trigger_dir, current, policy)
            if staged["research_queued"]:
                next_state["dedupe"][alert["key"]] = utc_iso(current)
            results.append({"key": alert["key"], "ok": True, **staged})
    resolution_ready_count = update_warning_resolutions(snapshot, policy, next_state, now=current)
    write_json(state_path, next_state)
    payload = {
        "version": "1.0",
        "analyzed_at": utc_iso(current),
        "candidate_count": len(alerts),
        "queued_count": 0,
        "research_queued_count": sum(1 for item in results if item.get("research_queued")),
        "resolution_ready_count": resolution_ready_count,
        "failed_count": sum(1 for item in results if not item.get("ok")),
        "alerts": alerts,
        "results": results,
        "price_only_alert_rule": "L2 enters warning chamber; substantive Google review must complete before external delivery",
        "actual_broker_writes": False,
    }
    write_json(OUTPUT_PATH, payload)
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", default=str(SNAPSHOT_PATH))
    parser.add_argument("--portfolio", default=str(PORTFOLIO_PATH))
    parser.add_argument("--policy", default=str(POLICY_PATH))
    parser.add_argument("--state", default=str(STATE_PATH))
    parser.add_argument("--enqueue", action="store_true")
    args = parser.parse_args()
    result = run_monitor(
        Path(args.snapshot), Path(args.portfolio), Path(args.policy), Path(args.state), enqueue=args.enqueue,
    )
    print(
        "MARKET_EMERGENCY_MONITOR "
        f"candidates={result['candidate_count']} queued={result['queued_count']} "
        f"failed={result['failed_count']} actual_broker_writes=false"
    )
    return 0 if result["failed_count"] == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
