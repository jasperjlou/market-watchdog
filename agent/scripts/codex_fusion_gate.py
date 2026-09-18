#!/usr/bin/env python3
"""Codex-side fusion gate for worker evidence and K-line context.

Default behavior writes draft source events under /app/agent/state only.
Committing into /app/state/source_events.json requires --commit-source-events
and ALLOW_CODEX_SOURCE_EVENT_COMMIT=1.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit


APP_DIR = Path(os.environ.get("APP_DIR", "/app"))
AGENT_DIR = APP_DIR / "agent"
RUNS_DIR = AGENT_DIR / "runs"
STATE_DIR = AGENT_DIR / "state"
DRAFT_PATH = STATE_DIR / "source_events_draft.json"
PROACTIVE_EVIDENCE_PATH = STATE_DIR / "proactive_news_evidence.json"
SOURCE_EVENTS_PATH = APP_DIR / "state" / "source_events.json"
PORTFOLIO_SNAPSHOT_PATH = STATE_DIR / "moomoo_portfolio_snapshot_latest.json"
HIT_SYMBOLS_PATH = STATE_DIR / "hit_symbols.json"
TRADING_LOCKS_PATH = STATE_DIR / "trading_locks.json"
RISK_CONTROL_PATH = APP_DIR / "state" / "risk_control_state.json"

SOURCE_SCORES = {"S0": 1.0, "S1": 0.75, "S2": 0.55, "S3": 0.25}
THRESHOLDS = {"L0": 0.35, "L1": 0.55, "L2": 0.70, "L3": 0.85, "L4": 0.85}
WEIGHTS = {
    "source_score": 0.35,
    "price_score": 0.20,
    "topic_score": 0.20,
    "corroboration_score": 0.15,
    "entity_score": 0.10,
}
DECISION_WEIGHTS = {
    "alert_score": 0.45,
    "insight_score": 0.12,
    "execution_readiness_score": 0.10,
    "risk_gate_score": 0.10,
    "protection_score": 0.06,
    "backtestability_score": 0.06,
    "data_quality_score": 0.05,
    "fundamental_score": 0.04,
    "strategy_validation_score": 0.02,
}
DECISION_ENGINE_VERSION = "0.4"
MIN_ACTIONABLE_DECISION_SCORE = 0.65
FUNDAMENTAL_TOPICS = {
    "earnings",
    "guidance",
    "revenue",
    "margin",
    "margins",
    "cash flow",
    "balance sheet",
    "valuation",
    "capital structure",
}
UNRESOLVED_CONTRADICTIONS = {"unresolved", "contradicted", "disputed", "denied", "conflicting"}


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def stable_id(*parts: Any) -> str:
    raw = "|".join(str(part or "") for part in parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def clamp01(value: float) -> float:
    return max(0.0, min(1.0, value))


def parse_utc(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def freshness(value: Any, now: datetime) -> tuple[float, str, float | None]:
    published = parse_utc(value)
    if published is None:
        return 0.25, "unverified", None
    age_minutes = (now - published).total_seconds() / 60
    if age_minutes < -5:
        return 0.25, "unverified", age_minutes
    age_minutes = max(0.0, age_minutes)
    if age_minutes <= 15:
        return 1.0, "fresh", age_minutes
    if age_minutes <= 60:
        return 0.90, "fresh", age_minutes
    if age_minutes <= 360:
        return 0.70, "aging", age_minutes
    if age_minutes <= 1440:
        return 0.45, "aging", age_minutes
    return 0.15, "stale", age_minutes


def canonical_url(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        parts = urlsplit(text)
    except ValueError:
        return text.lower().rstrip("/")
    if not parts.netloc:
        return text.lower().rstrip("/")
    path = parts.path.rstrip("/") or "/"
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, "", ""))


def evidence_identity(item: dict[str, Any]) -> str:
    url = canonical_url(item.get("url"))
    if url:
        return "url:" + url
    source = str(item.get("source_id") or item.get("source_name") or item.get("_worker") or "unknown").strip().lower()
    title = str(item.get("title") or "").strip().lower()
    return "source:" + source + ":" + stable_id(title)


def topic_key(item: dict[str, Any]) -> str:
    raw = item.get("topics") if isinstance(item.get("topics"), list) else []
    normalized = sorted({str(value).strip().lower() for value in raw if str(value).strip()})
    return ",".join(normalized)


def score_level(score: float, has_s0_s1: bool, has_price_resonance: bool, has_dual_official: bool, is_s3_only: bool) -> str:
    if score >= THRESHOLDS["L3"] and ((has_s0_s1 and has_price_resonance) or has_dual_official):
        level = "L3"
    elif score >= THRESHOLDS["L2"] and (has_s0_s1 or has_price_resonance):
        level = "L2"
    elif score >= THRESHOLDS["L1"]:
        level = "L1"
    else:
        level = "L0"
    if is_s3_only and level in {"L2", "L3"}:
        return "L1"
    return level


def level_rank(level: str) -> int:
    return {"L0": 0, "L1": 1, "L2": 2, "L3": 3, "L4": 4}.get(level, 0)


def level_from_rank(rank: int) -> str:
    return ["L0", "L1", "L2", "L3", "L4"][max(0, min(4, rank))]


def load_worker_items() -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    if RUNS_DIR.exists():
        for raw_path in sorted(RUNS_DIR.glob("*/raw_output.txt")):
            # Proactive feeds have a compact 7-day evidence index. Raw runs are audit-only
            # and are deliberately excluded from live scoring to prevent stale duplicates.
            if "_feed_" in raw_path.parent.name:
                continue
            raw = raw_path.read_text(encoding="utf-8", errors="replace").strip()
            if not raw:
                continue
            try:
                payload = json.loads(raw)
            except Exception:
                continue
            if isinstance(payload, dict) and isinstance(payload.get("items"), list):
                worker = str(payload.get("worker") or raw_path.parent.name)
                for item in payload["items"]:
                    if isinstance(item, dict):
                        normalized = dict(item)
                        normalized["_worker"] = worker
                        normalized["_run_dir"] = str(raw_path.parent)
                        items.append(normalized)

    proactive = load_json(PROACTIVE_EVIDENCE_PATH, {"items": []})
    for item in proactive.get("items", []) if isinstance(proactive, dict) else []:
        if isinstance(item, dict):
            normalized = dict(item)
            normalized["_worker"] = "proactive_public_feeds"
            normalized["_run_dir"] = str(PROACTIVE_EVIDENCE_PATH)
            items.append(normalized)

    deduped: dict[str, dict[str, Any]] = {}
    for item in items:
        deduped[evidence_identity(item)] = item
    return list(deduped.values())


def load_kline_by_symbol() -> dict[str, dict[str, Any]]:
    payload = load_json(STATE_DIR / "kline_snapshot_latest.json", {"items": []})
    result: dict[str, dict[str, Any]] = {}
    run_at = payload.get("run_at") if isinstance(payload, dict) else ""
    for item in payload.get("items", []) if isinstance(payload, dict) else []:
        if isinstance(item, dict) and item.get("symbol"):
            normalized = dict(item)
            normalized.setdefault("collected_at", run_at)
            result[str(item["symbol"]).upper()] = normalized
    return result


def load_portfolio_symbols() -> tuple[set[str], set[str]]:
    payload = load_json(PORTFOLIO_SNAPSHOT_PATH, {})
    holdings: set[str] = set()
    open_orders: set[str] = set()
    if not isinstance(payload, dict):
        return holdings, open_orders
    for item in payload.get("positions", []) if isinstance(payload.get("positions"), list) else []:
        if not isinstance(item, dict):
            continue
        contract = item.get("contract") if isinstance(item.get("contract"), dict) else {}
        symbol = str(item.get("symbol") or contract.get("symbol") or "").strip().upper()
        try:
            position = float(item.get("position") or 0)
        except Exception:
            position = 0.0
        if symbol and abs(position) > 0:
            holdings.add(symbol)
    for item in payload.get("open_orders", []) if isinstance(payload.get("open_orders"), list) else []:
        if not isinstance(item, dict):
            continue
        contract = item.get("contract") if isinstance(item.get("contract"), dict) else {}
        symbol = str(contract.get("symbol") or "").strip().upper()
        if symbol:
            open_orders.add(symbol)
    return holdings, open_orders


def load_risk_state() -> dict[str, Any]:
    locks = load_json(TRADING_LOCKS_PATH, {})
    risk = load_json(RISK_CONTROL_PATH, {})
    return {
        "locks": locks if isinstance(locks, dict) else {},
        "risk": risk if isinstance(risk, dict) else {},
    }


def tier(item: dict[str, Any]) -> str:
    value = str(item.get("source_tier") or "S3").upper()
    return value if value in SOURCE_SCORES else "S3"


def symbols(item: dict[str, Any]) -> list[str]:
    raw = item.get("symbols") or item.get("entities") or []
    if isinstance(raw, str):
        raw = [raw]
    return [str(value).upper() for value in raw if str(value).strip()]


def topic_score(item: dict[str, Any]) -> float:
    topics = item.get("topics")
    if isinstance(topics, list) and topics:
        return 0.75
    return 0.45


def entity_score(item: dict[str, Any]) -> float:
    return 0.80 if symbols(item) else 0.30


def insight_score(item: dict[str, Any]) -> float:
    confidence = str(item.get("confidence") or "").lower()
    topics = item.get("topics") if isinstance(item.get("topics"), list) else []
    summary = str(item.get("summary") or item.get("why_it_matters") or "")
    score = 0.35
    if confidence == "high":
        score += 0.25
    elif confidence == "medium":
        score += 0.15
    if topics:
        score += 0.15
    if any(word in summary.lower() for word in ["buy", "sell", "trim", "exit", "hedge", "upside", "downside", "guidance", "risk"]):
        score += 0.15
    if item.get("published_at"):
        score += 0.10
    return clamp01(score)


def execution_readiness_score(event_symbols: list[str], kline: dict[str, dict[str, Any]], has_portfolio_state: bool) -> float:
    has_price = any(symbol in kline for symbol in event_symbols)
    if has_price and has_portfolio_state:
        return 0.70
    if has_price:
        return 0.45
    if has_portfolio_state:
        return 0.30
    return 0.10


def risk_and_protection_scores(event_symbols: list[str], risk_state: dict[str, Any]) -> tuple[float, float, list[str], bool]:
    locks = risk_state.get("locks") if isinstance(risk_state.get("locks"), dict) else {}
    risk = risk_state.get("risk") if isinstance(risk_state.get("risk"), dict) else {}
    active_locks = []
    for symbol in event_symbols:
        symbol_locks = locks.get(symbol) or locks.get(symbol.upper()) if isinstance(locks, dict) else None
        if symbol_locks:
            active_locks.append(symbol)
    global_halt = str(risk.get("trading_state") or risk.get("status") or "").lower() in {"halted", "blocked", "red"}
    if global_halt:
        return 0.0, 0.0, active_locks, True
    if active_locks:
        return 0.35, 0.0, active_locks, False
    if risk:
        return 0.75, 0.75, active_locks, False
    return 0.55, 0.70, active_locks, False


def backtestability_score(item: dict[str, Any], event_symbols: list[str]) -> float:
    score = 0.20
    if item.get("url") or item.get("source_id"):
        score += 0.20
    if item.get("published_at"):
        score += 0.20
    if event_symbols:
        score += 0.20
    if item.get("topics"):
        score += 0.20
    return clamp01(score)


def fundamental_score(item: dict[str, Any]) -> float:
    topics = {str(value).strip().lower() for value in item.get("topics", []) if str(value).strip()} if isinstance(item.get("topics"), list) else set()
    matches = len(topics & FUNDAMENTAL_TOPICS)
    if matches >= 2:
        return 1.0
    if matches == 1:
        return 0.70
    return 0.30


def strategy_validation_score(item: dict[str, Any]) -> float:
    status = str(item.get("strategy_validation_status") or item.get("validation_status") or "").strip().lower()
    if status in {"validated", "backtested", "passed"}:
        return 1.0
    if status in {"partial", "paper", "simulated"}:
        return 0.60
    return 0.35


def unresolved_contradiction(item: dict[str, Any]) -> bool:
    status = str(item.get("contradiction_status") or "").strip().lower()
    return bool(
        status in UNRESOLVED_CONTRADICTIONS
        or item.get("has_unresolved_contradiction") is True
        or item.get("contradicted") is True
    )


def price_score(item: dict[str, Any], kline: dict[str, dict[str, Any]]) -> tuple[float, bool]:
    values = []
    for symbol in symbols(item):
        snapshot = kline.get(symbol)
        if not snapshot:
            continue
        if snapshot.get("price_resonance"):
            values.append(0.85)
        elif snapshot.get("trend_label") in {"uptrend", "downtrend"}:
            values.append(0.55)
        else:
            values.append(0.35)
    if not values:
        return 0.0, False
    score = max(values)
    return score, score >= 0.75


def corroboration_counts(items: list[dict[str, Any]]) -> dict[str, dict[str, str]]:
    counts: dict[str, dict[str, str]] = {}
    for item in items:
        identity = evidence_identity(item)
        current_tier = tier(item)
        for symbol in symbols(item):
            key = f"{symbol}:{topic_key(item)}"
            existing = counts.setdefault(key, {}).get(identity)
            if existing is None or SOURCE_SCORES[current_tier] > SOURCE_SCORES[existing]:
                counts[key][identity] = current_tier
    return counts


def corroboration_score(item: dict[str, Any], counts: dict[str, dict[str, str]]) -> tuple[float, bool, int, int]:
    best_independent = 0
    best_official = 0
    for symbol in symbols(item):
        key = f"{symbol}:{topic_key(item)}"
        evidence = counts.get(key, {})
        best_independent = max(best_independent, len(evidence))
        best_official = max(best_official, sum(1 for value in evidence.values() if value in {"S0", "S1"}))
    has_dual_official = best_official >= 2
    if has_dual_official:
        return 0.95, True, best_independent, best_official
    if best_independent >= 3:
        return 0.90, False, best_independent, best_official
    if best_independent == 2:
        return 0.65, False, best_independent, best_official
    return 0.10, False, best_independent, best_official


def normalize_event(
    item: dict[str, Any],
    kline: dict[str, dict[str, Any]],
    counts: dict[str, dict[str, str]],
    portfolio_symbols: set[str],
    open_order_symbols: set[str],
    risk_state: dict[str, Any],
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    current_tier = tier(item)
    source_score = SOURCE_SCORES[current_tier]
    p_score, has_price = price_score(item, kline)
    c_score, has_dual, independent_count, official_count = corroboration_score(item, counts)
    t_score = topic_score(item)
    e_score = entity_score(item)
    alert_score = round(
        clamp01(source_score) * WEIGHTS["source_score"]
        + clamp01(p_score) * WEIGHTS["price_score"]
        + clamp01(t_score) * WEIGHTS["topic_score"]
        + clamp01(c_score) * WEIGHTS["corroboration_score"]
        + clamp01(e_score) * WEIGHTS["entity_score"],
        4,
    )
    has_s0_s1 = current_tier in {"S0", "S1"}
    is_s3_only = current_tier == "S3"
    level = score_level(alert_score, has_s0_s1, has_price, has_dual, is_s3_only)
    event_symbols = symbols(item)
    freshness_score, freshness_status, evidence_age_minutes = freshness(item.get("published_at"), now)
    market_freshness_scores = [freshness(kline[symbol].get("collected_at"), now)[0] for symbol in event_symbols if symbol in kline]
    market_freshness_score = max(market_freshness_scores, default=0.0)
    has_fresh_market_context = market_freshness_score >= 0.90
    has_unresolved_contradiction = unresolved_contradiction(item)
    portfolio_hits = sorted(set(event_symbols) & portfolio_symbols)
    open_order_hits = sorted(set(event_symbols) & open_order_symbols)
    i_score = insight_score(item)
    x_score = execution_readiness_score(event_symbols, kline, bool(portfolio_symbols or open_order_symbols))
    r_score, protection_score, active_locks, hard_halt = risk_and_protection_scores(event_symbols, risk_state)
    b_score = backtestability_score(item, event_symbols)
    data_quality_score = round(clamp01(0.60 * freshness_score + 0.40 * market_freshness_score), 4)
    f_score = fundamental_score(item)
    s_score = strategy_validation_score(item)
    hard_penalty = 0.35 if hard_halt else (0.20 if active_locks else 0.0)
    if has_unresolved_contradiction:
        hard_penalty += 0.35
    if freshness_status == "stale":
        hard_penalty += 0.20
    elif freshness_status == "unverified":
        hard_penalty += 0.15
    decision_score = round(
        clamp01(
            DECISION_WEIGHTS["alert_score"] * alert_score
            + DECISION_WEIGHTS["insight_score"] * i_score
            + DECISION_WEIGHTS["execution_readiness_score"] * x_score
            + DECISION_WEIGHTS["risk_gate_score"] * r_score
            + DECISION_WEIGHTS["protection_score"] * protection_score
            + DECISION_WEIGHTS["backtestability_score"] * b_score
            + DECISION_WEIGHTS["data_quality_score"] * data_quality_score
            + DECISION_WEIGHTS["fundamental_score"] * f_score
            + DECISION_WEIGHTS["strategy_validation_score"] * s_score
            - hard_penalty
        ),
        4,
    )
    decision_reasons: list[str] = []
    if freshness_status == "stale":
        decision_reasons.append("stale_evidence")
        level = level_from_rank(level_rank(level) - 1)
    elif freshness_status == "unverified":
        decision_reasons.append("unverified_evidence_time")
        level = level_from_rank(level_rank(level) - 1)
    elif freshness_status == "aging":
        decision_reasons.append("aging_evidence")
    if not has_fresh_market_context:
        decision_reasons.append("stale_or_missing_market_context")
    if decision_score < MIN_ACTIONABLE_DECISION_SCORE and level_rank(level) > 2:
        decision_reasons.append("decision_score_below_actionable_threshold")
        level = "L2"

    high_actionability = (
        freshness_status == "fresh"
        and has_fresh_market_context
        and decision_score >= MIN_ACTIONABLE_DECISION_SCORE
        and not hard_halt
        and not active_locks
        and not has_unresolved_contradiction
    )
    if portfolio_hits or open_order_hits:
        decision_reasons.append("portfolio_or_open_order_hit")
        if level_rank(level) >= 3 and high_actionability:
            level = "L4"
        else:
            level = level_from_rank(min(3, level_rank(level) + 1))
    if hard_halt and level in {"L3", "L4"}:
        decision_reasons.append("hard_risk_halt")
        level = "L2"
    elif active_locks and level == "L4":
        decision_reasons.append("active_protection_lock")
        level = "L3"
    if has_unresolved_contradiction:
        decision_reasons.append("unresolved_contradiction")
        level = level_from_rank(min(1, level_rank(level)))
    order_ticket_draft_allowed = bool(
        level in {"L3", "L4"}
        and high_actionability
    )
    title = str(item.get("title") or "Untitled worker evidence")
    return {
        "decision_engine_version": DECISION_ENGINE_VERSION,
        "event_id": "agent_" + stable_id(title, item.get("url"), event_symbols, item.get("published_at")),
        "source_id": str(item.get("source_id") or item.get("_worker") or "agent_worker"),
        "source_name": str(item.get("source_name") or item.get("_worker") or "agent_worker"),
        "source_tier": current_tier,
        "priority": "P1" if current_tier in {"S0", "S1"} else "P2",
        "sector_id": "agent_news_trading",
        "title": title,
        "url": str(item.get("url") or ""),
        "published_at": str(item.get("published_at") or ""),
        "collected_at": now.replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "raw_summary": str(item.get("summary") or item.get("why_it_matters") or ""),
        "why_it_matters": str(item.get("why_it_matters") or ""),
        "limitations": str(item.get("limitations") or ""),
        "direction": str(item.get("direction") or item.get("market_direction") or "neutral"),
        "impact_horizon": str(item.get("impact_horizon") or "unspecified"),
        "entities": event_symbols,
        "topics": item.get("topics") if isinstance(item.get("topics"), list) else [],
        "inferred_event_type": "agent_worker_evidence",
        "evidence_kind": "material" if current_tier in {"S0", "S1"} else "context",
        "confidence": str(item.get("confidence") or "medium"),
        "source_score": source_score,
        "price_score": p_score,
        "topic_score": t_score,
        "corroboration_score": c_score,
        "entity_score": e_score,
        "alert_score": alert_score,
        "decision_score": decision_score,
        "insight_score": i_score,
        "execution_readiness_score": x_score,
        "risk_gate_score": r_score,
        "protection_score": protection_score,
        "backtestability_score": b_score,
        "data_quality_score": data_quality_score,
        "fundamental_score": f_score,
        "strategy_validation_score": s_score,
        "freshness_score": freshness_score,
        "freshness_status": freshness_status,
        "evidence_age_minutes": round(evidence_age_minutes, 2) if evidence_age_minutes is not None else None,
        "market_freshness_score": market_freshness_score,
        "has_fresh_market_context": has_fresh_market_context,
        "independent_source_count": independent_count,
        "official_source_count": official_count,
        "has_unresolved_contradiction": has_unresolved_contradiction,
        "decision_reasons": decision_reasons,
        "active_protection_locks": active_locks,
        "hard_risk_halt": hard_halt,
        "alert_level": level,
        "portfolio_hit": bool(portfolio_hits),
        "open_order_hit": bool(open_order_hits),
        "portfolio_hit_symbols": portfolio_hits,
        "open_order_hit_symbols": open_order_hits,
        "has_s0_s1": has_s0_s1,
        "has_price_resonance": has_price,
        "has_dual_official": has_dual,
        "is_s3_only": is_s3_only,
        "status": "draft_review",
        "trade_recommendation_allowed": True,
        "order_ticket_draft_allowed": order_ticket_draft_allowed,
        "cancel_replace_suggestion_allowed": bool(open_order_hits) and not has_unresolved_contradiction,
        "actual_broker_writes": False,
        "worker_run_dir": item.get("_run_dir", ""),
    }


def update_hit_symbols(items: list[dict[str, Any]], expiry_minutes: int = 90) -> int:
    now = datetime.now(timezone.utc)
    payload = load_json(HIT_SYMBOLS_PATH, {"version": "0.1", "items": []})
    old_items = payload.get("items") if isinstance(payload, dict) else []
    by_symbol: dict[str, dict[str, Any]] = {}
    if isinstance(old_items, list):
        for item in old_items:
            if not isinstance(item, dict):
                continue
            symbol = str(item.get("symbol") or "").strip().upper()
            if not symbol:
                continue
            expires_at_raw = str(item.get("expires_at") or "")
            if expires_at_raw:
                try:
                    expires_at = datetime.fromisoformat(expires_at_raw.replace("Z", "+00:00"))
                    if expires_at < now:
                        continue
                except Exception:
                    pass
            item["symbol"] = symbol
            by_symbol[symbol] = item

    added_or_updated = 0
    for item in items:
        if not isinstance(item, dict):
            continue
        symbols_to_track = set(item.get("portfolio_hit_symbols") or []) | set(item.get("open_order_hit_symbols") or [])
        if item.get("alert_level") == "L4":
            symbols_to_track |= set(item.get("entities") or [])
        for raw_symbol in symbols_to_track:
            symbol = str(raw_symbol).strip().upper()
            if not symbol:
                continue
            by_symbol[symbol] = {
                "symbol": symbol,
                "level": item.get("alert_level"),
                "reason": item.get("title"),
                "event_id": item.get("event_id"),
                "updated_at": utc_now_iso(),
                "expires_at": (now + timedelta(minutes=expiry_minutes)).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
                "source": "codex_fusion_gate",
            }
            added_or_updated += 1

    write_json(HIT_SYMBOLS_PATH, {"version": "0.1", "updated_at": utc_now_iso(), "items": sorted(by_symbol.values(), key=lambda x: x["symbol"])})
    return added_or_updated


def commit_events(draft: dict[str, Any]) -> int:
    if os.environ.get("ALLOW_CODEX_SOURCE_EVENT_COMMIT") != "1":
        raise PermissionError("ALLOW_CODEX_SOURCE_EVENT_COMMIT is not 1")
    existing = load_json(SOURCE_EVENTS_PATH, {"version": "0.1", "items": []})
    if not isinstance(existing, dict):
        existing = {"version": "0.1", "items": []}
    items = existing.get("items") if isinstance(existing.get("items"), list) else []
    by_id = {str(item.get("event_id")): item for item in items if isinstance(item, dict)}
    added = 0
    for item in draft.get("items", []):
        event_id = str(item.get("event_id"))
        if event_id not in by_id:
            by_id[event_id] = item
            added += 1
    existing["items"] = list(by_id.values())
    existing["updated_at"] = utc_now_iso()
    write_json(SOURCE_EVENTS_PATH, existing)
    return added


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--commit-source-events", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    ensure_dir(STATE_DIR)
    worker_items = load_worker_items()
    kline = load_kline_by_symbol()
    counts = corroboration_counts(worker_items)
    portfolio_symbols, open_order_symbols = load_portfolio_symbols()
    risk_state = load_risk_state()
    draft_items = [normalize_event(item, kline, counts, portfolio_symbols, open_order_symbols, risk_state) for item in worker_items]
    hit_updates = update_hit_symbols(draft_items)
    draft = {
        "version": DECISION_ENGINE_VERSION,
        "decision_engine_version": DECISION_ENGINE_VERSION,
        "updated_at": utc_now_iso(),
        "items": draft_items,
        "commit_source_events": False,
        "trade_recommendations_allowed": True,
        "order_ticket_drafts_allowed": True,
        "actual_broker_writes": False,
        "human_review_required": True,
        "portfolio_symbols_count": len(portfolio_symbols),
        "open_order_symbols_count": len(open_order_symbols),
        "hit_symbol_updates": hit_updates,
    }
    write_json(DRAFT_PATH, draft)

    added = 0
    if args.commit_source_events:
        added = commit_events(draft)
        draft["commit_source_events"] = True
        draft["committed_count"] = added
        write_json(DRAFT_PATH, draft)

    print(
        "CODEX_FUSION_GATE_OK "
        f"draft_items={len(draft_items)} draft_path={DRAFT_PATH} "
        f"commit={str(args.commit_source_events).lower()} committed={added} "
        f"hit_updates={hit_updates} actual_broker_writes=false"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
