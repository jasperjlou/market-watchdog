#!/usr/bin/env python3
"""Build explainable multi-horizon market outlooks from price, breadth and news.

The output is a directional decision aid, not a calibrated return forecast.  It
never calls broker APIs and never sends a message by itself.
"""

from __future__ import annotations

import argparse
import json
import math
import os
from datetime import datetime, timezone
from pathlib import Path
from statistics import median
from typing import Any


APP_DIR = Path(os.environ.get("APP_DIR", "/app"))
AGENT_DIR = Path(os.environ.get("AGENT_DIR", str(APP_DIR / "agent")))
STATE_DIR = AGENT_DIR / "state"
KLINE_PATH = STATE_DIR / "kline_snapshot_latest.json"
EVENTS_PATH = STATE_DIR / "source_events_draft.json"
PORTFOLIO_PATH = STATE_DIR / "moomoo_portfolio_snapshot_latest.json"
SCAN_POLICY_PATH = APP_DIR / "config" / "scan_policy.yaml"
OUTLOOK_POLICY_PATH = APP_DIR / "config" / "trend_outlook_policy.yaml"
OUTPUT_PATH = STATE_DIR / "trend_outlook_latest.json"

SOURCE_WEIGHTS = {"S0": 1.0, "S1": 0.8, "S2": 0.45, "S3": 0.2}
CONFIDENCE_WEIGHTS = {"high": 1.0, "medium": 0.7, "low": 0.4}
BENCHMARK_BY_THEME = {
    "broad_market": "SPY",
    "semiconductors": "SMH",
    "memory_storage": "SMH",
    "gold": "GLD",
    "space_aerospace": "XAR",
    "mega_cap_leaders": "QQQ",
}
POSITIVE_TERMS = (
    "raise guidance", "beat estimates", "contract award", "approval", "buyback",
    "price increase", "strong demand", "capacity expansion", "record revenue",
    "上调", "超预期", "中标", "获批", "回购", "需求强劲", "提价", "增长加速",
)
NEGATIVE_TERMS = (
    "cut guidance", "miss estimates", "delay", "launch failure", "investigation",
    "secondary offering", "export restriction", "downgrade", "weak demand",
    "下调", "不及预期", "延期", "发射失败", "调查", "禁令", "需求疲弱", "减产",
)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


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
    temporary.replace(path)


def safe_float(value: Any) -> float | None:
    try:
        parsed = float(value)
        return parsed if math.isfinite(parsed) else None
    except (TypeError, ValueError):
        return None


def clamp(value: float, low: float = -1.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def parse_time(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except ValueError:
        return None


def event_direction(item: dict[str, Any]) -> tuple[str, str]:
    explicit = str(item.get("direction") or item.get("market_direction") or "").strip().lower()
    aliases = {
        "positive": "bullish", "up": "bullish", "bullish": "bullish",
        "negative": "bearish", "down": "bearish", "bearish": "bearish",
        "mixed": "mixed", "neutral": "neutral",
    }
    if explicit in aliases:
        return aliases[explicit], "worker"
    text = " ".join(
        str(item.get(key) or "")
        for key in ("title", "raw_summary", "summary", "why_it_matters")
    ).lower()
    positive = sum(term in text for term in POSITIVE_TERMS)
    negative = sum(term in text for term in NEGATIVE_TERMS)
    if positive > negative:
        return "bullish", "keyword_inference"
    if negative > positive:
        return "bearish", "keyword_inference"
    return "neutral", "unresolved"


def event_age_hours(item: dict[str, Any], now: datetime) -> float | None:
    published = parse_time(item.get("published_at") or item.get("collected_at"))
    if published is None:
        return None
    return max(0.0, (now - published).total_seconds() / 3600.0)


def theme_memberships(scan_policy: dict[str, Any]) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
    by_symbol: dict[str, list[str]] = {}
    by_theme: dict[str, list[str]] = {}
    baskets = scan_policy.get("theme_baskets") if isinstance(scan_policy, dict) else {}
    for theme, config in baskets.items() if isinstance(baskets, dict) else []:
        values = config.get("symbols") if isinstance(config, dict) else []
        symbols = [str(value).upper() for value in values if str(value).strip()]
        by_theme[str(theme)] = symbols
        for symbol in symbols:
            by_symbol.setdefault(symbol, []).append(str(theme))
    return by_symbol, by_theme


def theme_stats(items: dict[str, dict[str, Any]], by_theme: dict[str, list[str]]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for theme, symbols in by_theme.items():
        members = [items[symbol] for symbol in symbols if symbol in items]
        returns_5d = [value for item in members if (value := safe_float(item.get("return_5d_pct"))) is not None]
        returns_20d = [value for item in members if (value := safe_float(item.get("return_20d_pct"))) is not None]
        median_5d = median(returns_5d) if returns_5d else None
        median_20d = median(returns_20d) if returns_20d else None
        up_5d = sum(value > 0 for value in returns_5d)
        breadth_5d = ((up_5d / len(returns_5d)) * 2.0 - 1.0) if returns_5d else 0.0
        short_score = clamp(0.55 * clamp((median_5d or 0.0) / 6.0) + 0.45 * breadth_5d)
        swing_score = clamp(0.65 * clamp((median_20d or 0.0) / 15.0) + 0.35 * breadth_5d)
        result[theme] = {
            "theme": theme,
            "member_count": len(members),
            "observed_5d_count": len(returns_5d),
            "median_return_5d_pct": round(median_5d, 3) if median_5d is not None else None,
            "median_return_20d_pct": round(median_20d, 3) if median_20d is not None else None,
            "up_ratio_5d": round(up_5d / len(returns_5d), 3) if returns_5d else None,
            "short_score": round(short_score, 4),
            "swing_score": round(swing_score, 4),
            "label": "偏强" if short_score >= 0.25 else ("偏弱" if short_score <= -0.25 else "分化/平稳"),
        }
    return result


def event_relevant(item: dict[str, Any], symbol: str, themes: list[str]) -> bool:
    entities = {str(value).strip().upper() for value in (item.get("entities") or item.get("symbols") or [])}
    if symbol in entities:
        return True
    topics = " ".join(str(value).lower() for value in (item.get("topics") or []))
    aliases = {
        "semiconductors": ("semiconductor", "chip", "hbm", "memory", "export control"),
        "memory_storage": ("memory", "storage", "hbm", "dram", "nand"),
        "gold": ("gold", "precious metal", "real yield", "inflation"),
        "space_aerospace": ("space", "aerospace", "defense", "launch", "nasa", "fcc"),
        "broad_market": ("macro", "rates", "fed", "market breadth", "liquidity"),
        "mega_cap_leaders": ("megacap", "ai", "cloud", "nasdaq"),
    }
    return any(any(alias in topics for alias in aliases.get(theme, (theme.replace("_", " "),))) for theme in themes)


def news_signal(
    symbol: str,
    themes: list[str],
    events: list[dict[str, Any]],
    policy: dict[str, Any],
    now: datetime,
) -> tuple[float, list[dict[str, Any]], bool]:
    news_policy = policy.get("news") if isinstance(policy.get("news"), dict) else {}
    lookback = float(news_policy.get("lookback_hours", 168))
    half_life = max(1.0, float(news_policy.get("decay_half_life_hours", 36)))
    source_weights = {**SOURCE_WEIGHTS, **(news_policy.get("source_weights") or {})}
    confidence_weights = {**CONFIDENCE_WEIGHTS, **(news_policy.get("confidence_weights") or {})}
    contributions: list[tuple[float, dict[str, Any]]] = []
    directions: set[str] = set()
    for item in events:
        if not isinstance(item, dict) or not event_relevant(item, symbol, themes):
            continue
        age = event_age_hours(item, now)
        if age is None or age > lookback:
            continue
        direction, direction_source = event_direction(item)
        sign = {"bullish": 1.0, "bearish": -1.0}.get(direction, 0.0)
        tier = str(item.get("source_tier") or "S3").upper()
        confidence = str(item.get("confidence") or "medium").lower()
        decay = 0.5 ** (age / half_life)
        weight = float(source_weights.get(tier, 0.2)) * float(confidence_weights.get(confidence, 0.4)) * decay
        contribution = sign * weight
        if direction in {"bullish", "bearish"}:
            directions.add(direction)
        digest = {
            "event_id": item.get("event_id"),
            "title": str(item.get("title") or "")[:180],
            "url": str(item.get("url") or ""),
            "source_name": str(item.get("source_name") or ""),
            "source_tier": tier,
            "published_at": str(item.get("published_at") or ""),
            "age_hours": round(age, 2),
            "direction": direction,
            "direction_source": direction_source,
            "impact_horizon": str(item.get("impact_horizon") or "unspecified"),
            "summary": str(item.get("raw_summary") or item.get("summary") or "")[:360],
            "title_zh": str(item.get("title_zh") or "")[:180],
            "summary_zh": str(item.get("summary_zh") or "")[:360],
            "entities": [str(value).upper() for value in (item.get("entities") or item.get("symbols") or []) if str(value).strip()],
            "topics": [str(value) for value in (item.get("topics") or []) if str(value).strip()],
            "weight": round(weight, 4),
        }
        contributions.append((contribution, digest))
    contributions.sort(key=lambda pair: abs(pair[0]), reverse=True)
    if not contributions:
        return 0.0, [], False
    numerator = sum(value for value, _item in contributions)
    denominator = sum(abs(value) for value, _item in contributions) or 1.0
    return round(clamp(numerator / denominator), 4), [item for _value, item in contributions[:6]], len(directions) > 1


def portfolio_by_symbol(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for item in payload.get("positions", []) if isinstance(payload, dict) else []:
        if not isinstance(item, dict):
            continue
        contract = item.get("contract") if isinstance(item.get("contract"), dict) else {}
        symbol = str(item.get("symbol") or contract.get("symbol") or "").strip().upper()
        if symbol:
            result[symbol] = item
    return result


def component_scores(
    item: dict[str, Any],
    benchmark: dict[str, Any] | None,
    breadth: dict[str, Any] | None,
) -> dict[str, float]:
    label = str(item.get("trend_label") or "")
    label_score = {
        "uptrend": 0.65, "downtrend": -0.65, "recovery_watch": 0.25,
        "pullback_watch": -0.25, "mixed": 0.0, "insufficient_data": 0.0,
    }.get(label, 0.0)
    vs20 = safe_float(item.get("close_vs_sma20_pct")) or 0.0
    vs60 = safe_float(item.get("close_vs_sma60_pct")) or 0.0
    short_structure = clamp(0.55 * clamp(vs20 / 6.0) + 0.45 * label_score)
    swing_structure = clamp(0.55 * clamp(vs60 / 12.0) + 0.45 * label_score)
    ret1 = safe_float(item.get("return_1d_pct")) or 0.0
    ret5 = safe_float(item.get("return_5d_pct")) or 0.0
    ret20 = safe_float(item.get("return_20d_pct")) or 0.0
    short_momentum = clamp(0.65 * clamp(ret5 / 8.0) + 0.35 * clamp(ret1 / 4.0))
    swing_momentum = clamp(0.70 * clamp(ret20 / 18.0) + 0.30 * clamp(ret5 / 8.0))
    benchmark5 = safe_float((benchmark or {}).get("return_5d_pct")) or 0.0
    benchmark20 = safe_float((benchmark or {}).get("return_20d_pct")) or 0.0
    return {
        "short_structure": short_structure,
        "swing_structure": swing_structure,
        "short_momentum": short_momentum,
        "swing_momentum": swing_momentum,
        "short_relative_strength": clamp((ret5 - benchmark5) / 8.0),
        "swing_relative_strength": clamp((ret20 - benchmark20) / 18.0),
        "short_breadth": float((breadth or {}).get("short_score") or 0.0),
        "swing_breadth": float((breadth or {}).get("swing_score") or 0.0),
    }


def risk_and_confidence(
    item: dict[str, Any], short_score: float, swing_score: float, news_conflict: bool,
    policy: dict[str, Any], has_news: bool,
) -> tuple[int, int, list[str]]:
    risk_policy = policy.get("risk") if isinstance(policy.get("risk"), dict) else {}
    scoring = policy.get("scoring") if isinstance(policy.get("scoring"), dict) else {}
    min_bars = int(scoring.get("minimum_history_bars", 20))
    reliable_bars = int(scoring.get("reliable_swing_history_bars", 60))
    bars = int(safe_float(item.get("bars")) or 0)
    atr = safe_float(item.get("atr_14_pct"))
    rsi = safe_float(item.get("rsi_14"))
    ret1 = abs(safe_float(item.get("return_1d_pct")) or 0.0)
    volz = safe_float(item.get("volume_z_20"))
    flags = {str(value) for value in (item.get("flags") or [])}
    reasons: list[str] = []
    risk = 5.0
    if bars < min_bars:
        risk += 45
        reasons.append("新上市/历史样本不足")
    elif bars < reliable_bars:
        risk += 15
        reasons.append("中期历史不足 60 根日线")
    if atr is not None and atr >= float(risk_policy.get("atr_extreme_pct", 8.0)):
        risk += 30
        reasons.append("波动率极高")
    elif atr is not None and atr >= float(risk_policy.get("atr_high_pct", 5.0)):
        risk += 18
        reasons.append("波动率偏高")
    if ret1 >= 8:
        risk += 20
        reasons.append("单日涨跌幅极端")
    elif ret1 >= 4:
        risk += 10
        reasons.append("单日波动较大")
    if volz is not None and volz >= 3:
        risk += 15
        reasons.append("成交量异常放大")
    if rsi is not None and (rsi >= float(risk_policy.get("overbought_rsi", 75)) or rsi <= float(risk_policy.get("oversold_rsi", 25))):
        risk += 10
        reasons.append("动量处于极端区")
    if "breakdown_20d" in flags:
        risk += 15
        reasons.append("跌破 20 日区间")
    if short_score * swing_score < -0.05:
        risk += 12
        reasons.append("短期与中期方向冲突")
    if news_conflict:
        risk += 10
        reasons.append("可靠消息方向相互冲突")
    if item.get("delayed") is True:
        risk += 8
        reasons.append("行情可能延迟")
    completeness = sum(item.get(key) is not None for key in ("return_5d_pct", "return_20d_pct", "sma_20", "sma_60", "rsi_14", "atr_14_pct")) / 6.0
    confidence = 20 + min(45, bars / max(reliable_bars, 1) * 45) + completeness * 25 + (10 if has_news else 0)
    if item.get("delayed") is True:
        confidence -= 10
    if bars < min_bars:
        confidence = min(confidence, 30)
    return int(round(clamp(risk, 0, 100))), int(round(clamp(confidence, 0, 100))), reasons


def heuristic_probabilities(score: float, confidence: int) -> dict[str, int]:
    effective = clamp(score) * (0.45 + 0.55 * confidence / 100.0)
    neutral = int(round(max(15.0, 35.0 - abs(effective) * 20.0)))
    directional = 100 - neutral
    up = int(round(directional * (effective + 1.0) / 2.0))
    up = max(0, min(directional, up))
    return {"up": up, "neutral": neutral, "down": directional - up}


def category_and_conclusion(
    item: dict[str, Any], short_score: float, swing_score: float, risk: int, policy: dict[str, Any]
) -> tuple[str, str]:
    scoring = policy.get("scoring") if isinstance(policy.get("scoring"), dict) else {}
    risk_policy = policy.get("risk") if isinstance(policy.get("risk"), dict) else {}
    bars = int(safe_float(item.get("bars")) or 0)
    if bars < int(scoring.get("minimum_history_bars", 20)):
        return "insufficient_history", "新上市或历史样本不足；只监测事件与异常风险，暂不形成可靠中期趋势结论"
    if risk >= int(risk_policy.get("high_risk_score", 70)):
        direction = "上行" if short_score > 0 else ("下行" if short_score < 0 else "震荡")
        return "high_risk", f"{direction}信号伴随高波动或冲突证据，优先控制风险并等待确认"
    stable = float(scoring.get("stable_score_threshold", 0.25))
    watch = float(scoring.get("watch_score_threshold", 0.38))
    elevated = int(risk_policy.get("elevated_risk_score", 45))
    if short_score >= stable and swing_score >= stable and risk < elevated:
        return "stable_up", "短中期结构与主题广度一致偏多，维持上行观察"
    if short_score <= -stable and swing_score <= -stable and risk < elevated:
        return "stable_down", "短中期结构与主题广度一致偏空，维持防守观察"
    if short_score >= watch or (short_score > 0.15 and swing_score > 0):
        return "watch_up", "上行倾向正在增强，但仍需新闻、广度或价格结构继续确认"
    if short_score <= -watch or (short_score < -0.15 and swing_score < 0):
        return "watch_down", "下行风险正在增强，关注支撑失守与负面事件延续"
    return "range_stable", "多空证据暂未形成一致方向，归为震荡/平稳"


def build_outlook(
    kline_payload: dict[str, Any], events_payload: dict[str, Any], portfolio_payload: dict[str, Any],
    scan_policy: dict[str, Any], outlook_policy: dict[str, Any], *, now: datetime | None = None,
) -> dict[str, Any]:
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    items = {
        str(item.get("symbol") or "").upper(): item
        for item in (kline_payload.get("items") or [])
        if isinstance(item, dict) and item.get("symbol")
    }
    events = [item for item in (events_payload.get("items") or []) if isinstance(item, dict)]
    by_symbol, by_theme = theme_memberships(scan_policy)
    themes = theme_stats(items, by_theme)
    holdings = portfolio_by_symbol(portfolio_payload)
    short_weights = (outlook_policy.get("scoring") or {}).get("short_term_weights") or {}
    swing_weights = (outlook_policy.get("scoring") or {}).get("swing_weights") or {}
    results: list[dict[str, Any]] = []
    all_news: dict[str, dict[str, Any]] = {}

    for symbol, item in sorted(items.items()):
        symbol_themes = by_symbol.get(symbol, [])
        primary_theme = next((theme for theme in symbol_themes if theme not in {"broad_market", "mega_cap_leaders"}), symbol_themes[0] if symbol_themes else "broad_market")
        benchmark_symbol = BENCHMARK_BY_THEME.get(primary_theme, "SPY")
        components = component_scores(item, items.get(benchmark_symbol), themes.get(primary_theme))
        news_score, news_items, news_conflict = news_signal(symbol, symbol_themes, events, outlook_policy, now)
        components["news"] = news_score
        short_score = clamp(
            components["short_structure"] * float(short_weights.get("trend_structure", 0.30))
            + components["short_momentum"] * float(short_weights.get("momentum", 0.25))
            + components["short_relative_strength"] * float(short_weights.get("relative_strength", 0.15))
            + components["short_breadth"] * float(short_weights.get("theme_breadth", 0.10))
            + news_score * float(short_weights.get("verified_news", 0.20))
        )
        swing_score = clamp(
            components["swing_structure"] * float(swing_weights.get("trend_structure", 0.35))
            + components["swing_momentum"] * float(swing_weights.get("momentum", 0.20))
            + components["swing_relative_strength"] * float(swing_weights.get("relative_strength", 0.15))
            + components["swing_breadth"] * float(swing_weights.get("theme_breadth", 0.10))
            + news_score * float(swing_weights.get("verified_news", 0.20))
        )
        risk, confidence, risk_reasons = risk_and_confidence(item, short_score, swing_score, news_conflict, outlook_policy, bool(news_items))
        category, conclusion = category_and_conclusion(item, short_score, swing_score, risk, outlook_policy)
        held = holdings.get(symbol)
        avg_cost = safe_float(
            (held or {}).get("average_cost")
            if "average_cost" in (held or {})
            else (held or {}).get("avg_cost")
        )
        last_close = safe_float(item.get("last_close"))
        unrealized = ((last_close / avg_cost - 1.0) * 100.0) if avg_cost and last_close else None
        risk_policy = outlook_policy.get("risk") if isinstance(outlook_policy.get("risk"), dict) else {}
        portfolio_review = "none"
        if unrealized is not None and unrealized >= float(risk_policy.get("portfolio_profit_review_pct", 15.0)):
            portfolio_review = "profit_review"
        elif unrealized is not None and unrealized <= float(risk_policy.get("portfolio_loss_review_pct", -8.0)):
            portfolio_review = "loss_review"
        invalidation = "跌破并连续收在 SMA20 下方" if short_score > 0.15 else ("重新站上并连续收在 SMA20 上方" if short_score < -0.15 else "等待突破近期区间")
        result = {
            "symbol": symbol,
            "scan_tier": item.get("scan_tier"),
            "primary_theme": primary_theme,
            "themes": symbol_themes,
            "benchmark": benchmark_symbol,
            "category": category,
            "conclusion": conclusion,
            "short_term": {
                "horizon": "1-5 trading days",
                "direction_score": round(short_score, 4),
                "direction": "偏多" if short_score >= 0.15 else ("偏空" if short_score <= -0.15 else "震荡"),
                "heuristic_probability_pct": heuristic_probabilities(short_score, confidence),
            },
            "swing": {
                "horizon": "2-6 weeks",
                "direction_score": round(swing_score, 4),
                "direction": "偏多" if swing_score >= 0.15 else ("偏空" if swing_score <= -0.15 else "震荡"),
                "heuristic_probability_pct": heuristic_probabilities(swing_score, confidence),
            },
            "confidence": confidence,
            "confidence_label": "高" if confidence >= 75 else ("中" if confidence >= 50 else "低"),
            "risk_score": risk,
            "risk_reasons": risk_reasons,
            "news_score": news_score,
            "news_conflict": news_conflict,
            "news": news_items,
            "components": {key: round(value, 4) for key, value in components.items()},
            "last_close": last_close,
            "return_1d_pct": safe_float(item.get("return_1d_pct")),
            "return_5d_pct": safe_float(item.get("return_5d_pct")),
            "return_20d_pct": safe_float(item.get("return_20d_pct")),
            "bars": int(safe_float(item.get("bars")) or 0),
            "data_source": item.get("source"),
            "delayed": item.get("delayed"),
            "invalidation": invalidation,
            "portfolio": {
                "held": bool(held),
                "position": (held or {}).get("position"),
                "avg_cost": avg_cost,
                "unrealized_pct": round(unrealized, 2) if unrealized is not None else None,
                "review": portfolio_review,
            },
            "actual_broker_writes": False,
        }
        results.append(result)
        for news in news_items:
            key = str(news.get("event_id") or news.get("url") or news.get("title"))
            all_news[key] = news

    category_order = {"high_risk": 0, "insufficient_history": 1, "watch_down": 2, "watch_up": 3, "stable_down": 4, "stable_up": 5, "range_stable": 6}
    results.sort(key=lambda item: (category_order.get(str(item["category"]), 99), -int(item["risk_score"]), item["symbol"]))
    category_counts: dict[str, int] = {}
    for item in results:
        category_counts[item["category"]] = category_counts.get(item["category"], 0) + 1
    tier_rank = {"S0": 0, "S1": 1, "S2": 2, "S3": 3}
    news_digest = sorted(
        all_news.values(),
        key=lambda item: (
            str(item.get("direction")) not in {"bullish", "bearish", "mixed"},
            tier_rank.get(str(item.get("source_tier") or "S3"), 3),
            item.get("age_hours") is None,
            item.get("age_hours") or 0.0,
        ),
    )[:12]
    return {
        "version": "1.0",
        "generated_at": now.replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "positioning": str(outlook_policy.get("positioning") or "explainable probabilistic trend outlook"),
        "forecast_kind": "heuristic_directional_probability_not_calibrated_return_forecast",
        "price_snapshot_at": kline_payload.get("run_at"),
        "news_event_count": len(events),
        "news_digest_count": len(news_digest),
        "category_counts": category_counts,
        "themes": list(themes.values()),
        "items": results,
        "news_digest": news_digest,
        "readonly": True,
        "auto_trade_allowed": False,
        "actual_broker_writes": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kline", default=str(KLINE_PATH))
    parser.add_argument("--events", default=str(EVENTS_PATH))
    parser.add_argument("--portfolio", default=str(PORTFOLIO_PATH))
    parser.add_argument("--scan-policy", default=str(SCAN_POLICY_PATH))
    parser.add_argument("--outlook-policy", default=str(OUTLOOK_POLICY_PATH))
    parser.add_argument("--output", default=str(OUTPUT_PATH))
    args = parser.parse_args()

    payload = build_outlook(
        load_json(Path(args.kline), {"items": []}),
        load_json(Path(args.events), {"items": []}),
        load_json(Path(args.portfolio), {"positions": []}),
        load_yaml(Path(args.scan_policy)),
        load_yaml(Path(args.outlook_policy)),
    )
    write_json(Path(args.output), payload)
    print(
        "TREND_OUTLOOK "
        f"items={len(payload['items'])} news={payload['news_event_count']} "
        f"categories={json.dumps(payload['category_counts'], ensure_ascii=False, separators=(',', ':'))} "
        "actual_broker_writes=false"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
