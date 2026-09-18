from __future__ import annotations

import importlib.util
from datetime import datetime, timezone
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "trend_outlook_engine.py"
NOW = datetime(2026, 7, 14, 18, 0, tzinfo=timezone.utc)


def load_module():
    spec = importlib.util.spec_from_file_location("trend_outlook_engine", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def policy() -> dict:
    return {
        "positioning": "test",
        "news": {
            "lookback_hours": 168,
            "decay_half_life_hours": 36,
            "source_weights": {"S0": 1.0, "S1": 0.8, "S2": 0.45, "S3": 0.2},
            "confidence_weights": {"high": 1.0, "medium": 0.7, "low": 0.4},
        },
        "scoring": {
            "short_term_weights": {"trend_structure": 0.3, "momentum": 0.25, "relative_strength": 0.15, "theme_breadth": 0.1, "verified_news": 0.2},
            "swing_weights": {"trend_structure": 0.35, "momentum": 0.2, "relative_strength": 0.15, "theme_breadth": 0.1, "verified_news": 0.2},
            "stable_score_threshold": 0.25,
            "watch_score_threshold": 0.38,
            "minimum_history_bars": 20,
            "reliable_swing_history_bars": 60,
        },
        "risk": {"high_risk_score": 70, "elevated_risk_score": 45, "atr_high_pct": 5, "atr_extreme_pct": 8, "overbought_rsi": 75, "oversold_rsi": 25, "portfolio_profit_review_pct": 15, "portfolio_loss_review_pct": -8},
    }


def price(symbol: str, *, bars: int = 100, ret1: float = 1.0, ret5: float = 6.0, ret20: float = 12.0) -> dict:
    return {
        "symbol": symbol,
        "scan_tier": "themes",
        "bars": bars,
        "last_close": 120,
        "return_1d_pct": ret1,
        "return_5d_pct": ret5 if bars >= 6 else None,
        "return_20d_pct": ret20 if bars >= 21 else None,
        "sma_20": 110,
        "sma_60": 100,
        "close_vs_sma20_pct": 9.1,
        "close_vs_sma60_pct": 20,
        "rsi_14": 62 if bars >= 15 else None,
        "atr_14_pct": 2 if bars >= 15 else None,
        "volume_z_20": 0.5 if bars >= 21 else None,
        "trend_label": "uptrend" if bars >= 60 else "insufficient_data",
        "flags": [],
        "source": "yfinance",
        "delayed": False,
    }


def test_build_outlook_classifies_trend_and_new_listing_guard() -> None:
    module = load_module()
    kline = {"run_at": "2026-07-14T17:59:00Z", "items": [price("SMH"), price("MU"), price("SKHY", bars=3, ret1=8)]}
    scan_policy = {"theme_baskets": {"semiconductors": {"symbols": ["SMH", "MU", "SKHY"]}, "memory_storage": {"symbols": ["MU", "SKHY"]}}}

    result = module.build_outlook(kline, {"items": []}, {"positions": []}, scan_policy, policy(), now=NOW)
    by_symbol = {item["symbol"]: item for item in result["items"]}

    assert by_symbol["MU"]["category"] == "stable_up"
    assert by_symbol["MU"]["short_term"]["heuristic_probability_pct"]["up"] > by_symbol["MU"]["short_term"]["heuristic_probability_pct"]["down"]
    assert by_symbol["SKHY"]["category"] == "insufficient_history"
    assert by_symbol["SKHY"]["confidence"] <= 30


def test_verified_news_is_independent_forward_input_and_portfolio_review() -> None:
    module = load_module()
    neutral = price("MU", ret1=0, ret5=0, ret20=0)
    neutral.update({"last_close": 120, "sma_20": 120, "sma_60": 120, "close_vs_sma20_pct": 0, "close_vs_sma60_pct": 0, "trend_label": "mixed"})
    event = {
        "event_id": "e1", "title": "Issuer cuts guidance", "url": "https://issuer.example/release",
        "source_name": "issuer", "source_tier": "S0", "published_at": "2026-07-14T17:30:00Z",
        "entities": ["MU"], "topics": ["memory"], "raw_summary": "Guidance was cut.",
        "direction": "bearish", "impact_horizon": "2_6w", "confidence": "high",
    }
    portfolio = {"positions": [{"contract": {"symbol": "MU"}, "position": 10, "avg_cost": 100}]}
    scan_policy = {"theme_baskets": {"memory_storage": {"symbols": ["MU"]}}}

    result = module.build_outlook({"items": [neutral]}, {"items": [event]}, portfolio, scan_policy, policy(), now=NOW)
    item = result["items"][0]

    assert item["news_score"] < 0
    assert item["short_term"]["direction_score"] < 0
    assert item["portfolio"]["review"] == "profit_review"
    assert item["news"][0]["impact_horizon"] == "2_6w"
    assert item["news"][0]["entities"] == ["MU"]
    assert item["news"][0]["topics"] == ["memory"]
    assert result["news_digest_count"] == 1
