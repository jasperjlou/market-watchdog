from __future__ import annotations

import importlib.util
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path


AGENT_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = AGENT_ROOT / "scripts" / "market_signal_engine.py"


def load_module():
    spec = importlib.util.spec_from_file_location("market_signal_engine_test", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_theme_resonance_is_price_only_l1_and_requires_confirmation() -> None:
    module = load_module()
    snapshot = {
        "items": [
            {"symbol": "MU", "return_1d_pct": 3.2, "flags": ["intraday_move"]},
            {"symbol": "WDC", "return_1d_pct": 2.8, "flags": ["intraday_move"]},
            {"symbol": "STX", "return_1d_pct": 0.4, "flags": []},
        ]
    }
    policy = {
        "theme_baskets": {
            "memory_storage": {
                "symbols": ["MU", "WDC", "STX"],
                "minimum_confirming_symbols": 2,
                "median_move_threshold_pct": 2.0,
            }
        }
    }

    candidates = module.analyze(snapshot, policy)
    theme = next(item for item in candidates if item["kind"] == "theme_resonance")

    assert theme["direction"] == "bullish"
    assert theme["candidate_level"] == "L1"
    assert theme["requires_confirmation_for_L2"] is True


def test_dedupe_cooldown_blocks_recent_candidate_only() -> None:
    module = load_module()
    now = datetime(2026, 7, 14, tzinfo=timezone.utc)
    candidates = [{"key": "one"}, {"key": "two"}]
    state = {"history": {"one": module.utc_iso(now - timedelta(minutes=30))}}

    due = module.due_candidates(candidates, state, now, 60)

    assert due == [{"key": "two"}]


def test_theme_candidates_are_prioritized_before_single_ticker_noise() -> None:
    module = load_module()
    theme = {"kind": "theme_resonance", "confirming_symbols": 3, "median_move_pct": 2.0}
    single = {"kind": "single_symbol_anomaly", "flags": ["intraday_move"], "return_1d_pct": 4.1}

    assert module.priority_score(theme) > module.priority_score(single)


def test_snapshot_fingerprint_ignores_collection_time_but_tracks_market_values() -> None:
    module = load_module()
    first = {"run_at": "2026-07-18T01:00:00Z", "items": [{
        "symbol": "MU", "collected_at": "2026-07-18T01:00:00Z",
        "provider_update_time": "2026-07-17T20:00:00Z", "last_close": 120,
        "return_1d_pct": 5.1, "flags": ["intraday_move"],
    }]}
    same_market_data = {"run_at": "2026-07-18T01:05:00Z", "items": [{
        **first["items"][0], "collected_at": "2026-07-18T01:05:00Z",
    }]}
    changed_market_data = {"items": [{**first["items"][0], "last_close": 121}]}

    assert module.snapshot_fingerprint(first) == module.snapshot_fingerprint(same_market_data)
    assert module.snapshot_fingerprint(first) != module.snapshot_fingerprint(changed_market_data)


def test_unchanged_snapshot_is_suppressed_and_hourly_budget_is_global() -> None:
    module = load_module()
    now = datetime(2026, 7, 18, 1, 0, tzinfo=timezone.utc)
    snapshot = {"items": [{"symbol": "MU", "last_close": 120, "flags": ["intraday_move"]}]}
    candidates = [
        {"key": f"symbol:S{index}:bullish", "kind": "single_symbol_anomaly", "flags": ["intraday_move"], "return_1d_pct": 5 + index}
        for index in range(4)
    ]
    fingerprint = module.candidate_fingerprint(candidates)

    unchanged = module.select_for_enqueue(
        candidates, {"last_candidate_fingerprint": fingerprint}, snapshot, now,
        cooldown_minutes=60, max_per_cycle=4, max_per_hour=4,
    )
    assert unchanged["selected"] == []
    assert unchanged["unchanged_snapshot_suppressed_count"] == 4

    budgeted = module.select_for_enqueue(
        candidates,
        {"recent_trigger_times": [module.utc_iso(now - timedelta(minutes=10))] * 3},
        snapshot, now, cooldown_minutes=60, max_per_cycle=4, max_per_hour=4,
    )
    assert len(budgeted["selected"]) == 1
    assert budgeted["hourly_budget_remaining"] == 1


def test_candidate_fingerprint_ignores_small_amplitude_recalculation() -> None:
    module = load_module()
    first = [{
        "key": "symbol:MU:bullish", "kind": "single_symbol_anomaly",
        "direction": "bullish", "symbols": ["MU"],
        "return_1d_pct": 5.1, "flags": ["intraday_move"],
    }]
    recalculated = [{**first[0], "return_1d_pct": 5.3}]
    materially_changed = [{**first[0], "flags": ["breakout_20d", "intraday_move"]}]

    assert module.candidate_fingerprint(first) == module.candidate_fingerprint(recalculated)
    assert module.candidate_fingerprint(first) != module.candidate_fingerprint(materially_changed)


def test_l1_enqueue_never_executes_ai_workers(monkeypatch, tmp_path: Path) -> None:
    module = load_module()
    monkeypatch.setattr(module, "TRIGGER_DIR", tmp_path)
    now = datetime(2026, 8, 14, 12, 0, tzinfo=timezone.utc)

    path = Path(module.enqueue({
        "key": "symbol:MU:bullish",
        "symbols": ["MU"],
    }, now))
    payload = json.loads(path.read_text(encoding="utf-8"))

    assert payload["price_only_level_cap"] == "L1"
    assert payload["execute_workers"] is False
