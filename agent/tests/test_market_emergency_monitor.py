from __future__ import annotations

import importlib.util
from datetime import datetime, timedelta, timezone
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "market_emergency_monitor.py"
NOW = datetime(2026, 7, 14, 18, 0, tzinfo=timezone.utc)


def load_module():
    spec = importlib.util.spec_from_file_location("market_emergency_monitor", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


POLICY = {
    "emergency": {
        "dedupe_minutes": 30, "confirmation_window_minutes": 20,
        "single_stock_immediate_move_pct": 8, "core_or_etf_immediate_move_pct": 3,
        "single_stock_confirmed_move_pct": 5, "core_or_etf_confirmed_move_pct": 2,
        "new_listing_immediate_move_pct": 15,
        "symbol_threshold_overrides": {"^VIX": {"immediate_move_pct": 10, "confirmed_move_pct": 6}},
    },
    "risk": {"portfolio_profit_review_pct": 15, "portfolio_loss_review_pct": -8},
}


def item(symbol: str, move: float, *, bars: int = 100, marker: str = "t1", resonance: bool = True) -> dict:
    return {"symbol": symbol, "return_1d_pct": move, "bars": bars, "scan_tier": "themes", "collected_at": marker, "price_resonance": resonance, "flags": ["volume_spike"] if resonance else [], "last_close": 100}


def test_extreme_move_alerts_immediately_but_new_listing_has_higher_guard() -> None:
    module = load_module()
    alerts, _ = module.analyze({"items": [item("MU", 9), item("SKHY", 9, bars=3)]}, {"positions": []}, POLICY, {}, now=NOW)
    symbols = {alert["symbol"] for alert in alerts}
    assert "MU" in symbols
    assert "SKHY" not in symbols

    alerts, _ = module.analyze({"items": [item("SKHY", 16, bars=3)]}, {"positions": []}, POLICY, {}, now=NOW)
    assert alerts[0]["kind"] == "new_listing_extreme"


def test_confirmed_move_requires_two_distinct_snapshots() -> None:
    module = load_module()
    first_snapshot = {"items": [item("RKLB", -6, marker="t1", resonance=True)]}
    first, state = module.analyze(first_snapshot, {"positions": []}, POLICY, {}, now=NOW)
    assert first == []

    duplicate, state = module.analyze(first_snapshot, {"positions": []}, POLICY, state, now=NOW + timedelta(minutes=1))
    assert duplicate == []
    assert state["observations"]["RKLB"]["count"] == 1

    second_snapshot = {"items": [item("RKLB", -6.5, marker="t2", resonance=True)]}
    confirmed, state = module.analyze(second_snapshot, {"positions": []}, POLICY, state, now=NOW + timedelta(minutes=5))
    assert confirmed[0]["kind"] == "confirmed_severe_move"
    assert confirmed[0]["cause_status"] == "unconfirmed_pending_news"


def test_portfolio_threshold_crossing_is_review_only() -> None:
    module = load_module()
    snapshot = {"items": [{**item("MU", 1), "last_close": 120}]}
    portfolio = {"positions": [{"contract": {"symbol": "MU"}, "position": 2, "avg_cost": 100}]}
    alerts, _ = module.analyze(snapshot, portfolio, POLICY, {}, now=NOW)
    review = next(alert for alert in alerts if alert["kind"] == "profit_review")
    assert "复核" in review["action"]
    assert review["alert_level"] == "L2"


def test_moomoo_portfolio_schema_crosses_the_same_read_only_threshold() -> None:
    module = load_module()
    snapshot = {"items": [{**item("MU", 1), "last_close": 120}]}
    portfolio = {
        "provider": "moomoo",
        "readonly": True,
        "actual_broker_writes": False,
        "positions": [{"symbol": "MU", "position": 2, "average_cost": 100}],
    }

    alerts, _ = module.analyze(snapshot, portfolio, POLICY, {}, now=NOW)

    review = next(alert for alert in alerts if alert["kind"] == "profit_review")
    assert review["symbol"] == "MU"
    assert review["alert_level"] == "L2"


def test_production_portfolio_source_is_moomoo() -> None:
    module = load_module()
    assert module.PORTFOLIO_PATH.name == "moomoo_portfolio_snapshot_latest.json"


def test_volatility_index_uses_asset_specific_threshold() -> None:
    module = load_module()
    ordinary, _ = module.analyze({"items": [{**item("^VIX", -4), "scan_tier": "core"}]}, {"positions": []}, POLICY, {}, now=NOW)
    extreme, _ = module.analyze({"items": [{**item("^VIX", -11), "scan_tier": "core"}]}, {"positions": []}, POLICY, {}, now=NOW)

    assert ordinary == []
    assert extreme[0]["symbol"] == "^VIX"


def test_l2_enters_warning_chamber_before_any_external_message(tmp_path: Path) -> None:
    module = load_module()
    snapshot_path = tmp_path / "snapshot.json"
    portfolio_path = tmp_path / "portfolio.json"
    policy_path = tmp_path / "policy.yaml"
    state_path = tmp_path / "state.json"
    trigger_dir = tmp_path / "triggers"
    snapshot_path.write_text(
        __import__("json").dumps({"items": [item("MU", 9.0, marker="t1")]}),
        encoding="utf-8",
    )
    portfolio_path.write_text('{"positions":[]}', encoding="utf-8")
    policy_path.write_text(
        "emergency:\n"
        "  single_stock_immediate_move_pct: 8\n"
        "  core_or_etf_immediate_move_pct: 3\n"
        "  single_stock_confirmed_move_pct: 5\n"
        "  core_or_etf_confirmed_move_pct: 2\n"
        "  new_listing_immediate_move_pct: 15\n"
        "  confirmation_window_minutes: 20\n",
        encoding="utf-8",
    )
    sent: list[tuple[str, str, str]] = []

    result = module.run_monitor(
        snapshot_path,
        portfolio_path,
        policy_path,
        state_path,
        enqueue=True,
        now=NOW,
        sender=lambda subject, body, priority: sent.append((subject, body, priority)) or {"ok": True},
        trigger_dir=trigger_dir,
    )

    state = module.load_json(state_path, {})
    warning = state["warning_chamber"]["MU"]
    assert sent == []
    assert result["queued_count"] == 0
    assert result["research_queued_count"] == 1
    assert warning["status"] == "research_pending"
    assert warning["revision"] == 1
    assert len(list(trigger_dir.glob("*.json"))) == 1

    repeated = module.run_monitor(
        snapshot_path,
        portfolio_path,
        policy_path,
        state_path,
        enqueue=True,
        now=NOW + timedelta(minutes=1),
        sender=lambda subject, body, priority: sent.append((subject, body, priority)) or {"ok": True},
        trigger_dir=trigger_dir,
    )

    assert repeated["research_queued_count"] == 0
    assert sent == []
    assert len(list(trigger_dir.glob("*.json"))) == 1


def test_active_warning_reopens_review_only_after_material_worsening(tmp_path: Path) -> None:
    module = load_module()
    snapshot_path = tmp_path / "snapshot.json"
    portfolio_path = tmp_path / "portfolio.json"
    policy_path = tmp_path / "policy.yaml"
    state_path = tmp_path / "state.json"
    trigger_dir = tmp_path / "triggers"
    portfolio_path.write_text('{"positions":[]}', encoding="utf-8")
    policy_path.write_text(
        "emergency:\n"
        "  single_stock_immediate_move_pct: 8\n"
        "  core_or_etf_immediate_move_pct: 3\n"
        "  single_stock_confirmed_move_pct: 5\n"
        "  core_or_etf_confirmed_move_pct: 2\n"
        "  new_listing_immediate_move_pct: 15\n"
        "  confirmation_window_minutes: 20\n"
        "  renotify_move_delta_pct: 5\n",
        encoding="utf-8",
    )
    snapshot_path.write_text(__import__("json").dumps({"items": [item("MU", 9, marker="t1")]}), encoding="utf-8")
    module.run_monitor(snapshot_path, portfolio_path, policy_path, state_path, enqueue=True, now=NOW, trigger_dir=trigger_dir)
    state = module.load_json(state_path, {})
    state["warning_chamber"]["MU"].update({"status": "active_warning", "last_reviewed_move_pct": 9})
    module.write_json(state_path, state)

    snapshot_path.write_text(__import__("json").dumps({"items": [item("MU", 10.5, marker="t2")]}), encoding="utf-8")
    small = module.run_monitor(snapshot_path, portfolio_path, policy_path, state_path, enqueue=True, now=NOW + timedelta(minutes=1), trigger_dir=trigger_dir)
    snapshot_path.write_text(__import__("json").dumps({"items": [item("MU", 14.2, marker="t3")]}), encoding="utf-8")
    worse = module.run_monitor(snapshot_path, portfolio_path, policy_path, state_path, enqueue=True, now=NOW + timedelta(minutes=2), trigger_dir=trigger_dir)

    assert small["research_queued_count"] == 0
    assert worse["research_queued_count"] == 1
    assert module.load_json(state_path, {})["warning_chamber"]["MU"]["revision"] == 2


def test_active_warning_requires_two_calm_snapshots_before_resolution(tmp_path: Path) -> None:
    module = load_module()
    state = {
        "warning_chamber": {"MU": {
            "symbol": "MU", "status": "active_warning", "revision": 1,
            "direction": "up", "move_pct": 9, "last_reviewed_move_pct": 9,
        }},
        "observations": {}, "dedupe": {}, "portfolio_bands": {},
    }
    calm1 = {"items": [item("MU", 1.0, marker="calm1", resonance=False)]}
    calm2 = {"items": [item("MU", 0.8, marker="calm2", resonance=False)]}

    _, after_one = module.analyze(calm1, {"positions": []}, POLICY, state, now=NOW)
    first = module.update_warning_resolutions(calm1, POLICY, after_one, now=NOW)
    _, after_two = module.analyze(calm2, {"positions": []}, POLICY, after_one, now=NOW + timedelta(minutes=5))
    second = module.update_warning_resolutions(calm2, POLICY, after_two, now=NOW + timedelta(minutes=5))

    assert first == 0
    assert second == 1
    assert after_two["warning_chamber"]["MU"]["status"] == "resolution_pending"
