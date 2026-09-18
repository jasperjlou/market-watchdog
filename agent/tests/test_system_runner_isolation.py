from __future__ import annotations

import importlib.util
from datetime import datetime, timedelta, timezone
from pathlib import Path


AGENT_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = AGENT_ROOT / "scripts" / "system_runner.py"


def load_module():
    spec = importlib.util.spec_from_file_location("system_runner", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_optional_broker_and_gui_failures_do_not_make_core_unstable() -> None:
    module = load_module()
    ports = {
        "moomoo_opend": {"ok": False},
        "novnc": {"ok": False},
        "wechat_callback": {"ok": True},
    }
    processes = {
        "ib_gateway_java": False,
        "wechat_callback": True,
        "xvfb": False,
        "x11vnc": False,
        "websockify": False,
    }
    policy = {
        "required_ports": ["wechat_callback"],
        "required_processes": ["wechat_callback"],
    }

    result = module.compute_stability(ports, processes, policy)

    assert result["stable"] is True
    assert result["required_failures"] == []
    assert set(result["optional_failures"]) == {
        "port:moomoo_opend",
        "port:novnc",
        "process:ib_gateway_java",
        "process:xvfb",
        "process:x11vnc",
        "process:websockify",
    }
    assert all(name != "vnc" for name, _host, _port in module.PORTS)


def test_runner_uses_moomoo_for_portfolio_and_polls_both_chat_channels() -> None:
    text = SCRIPT.read_text(encoding="utf-8")

    assert "moomoo_portfolio_snapshot.py" in text
    assert "ibkr_portfolio_snapshot.py" not in text
    assert "def run_telegram_ai_poller" in text
    assert 'last["telegram_ai"] = run_telegram_ai_poller()' in text
    for relative in (
        "codex_fusion_gate.py",
        "trend_outlook_engine.py",
        "market_emergency_monitor.py",
        "warning_review_dispatcher.py",
    ):
        consumer = AGENT_ROOT / "scripts" / relative
        consumer_text = consumer.read_text(encoding="utf-8")
        assert "moomoo_portfolio_snapshot_latest.json" in consumer_text


def test_health_transition_only_alerts_on_degrade_and_recovery() -> None:
    module = load_module()
    healthy = {"stable": True, "startup_ready": True, "required_service_failures": []}
    degraded = {"stable": False, "startup_ready": True, "required_service_failures": ["port:wechat_callback"]}

    assert module.health_transition({}, healthy) is None
    down = module.health_transition({"health": "healthy"}, degraded)
    assert down and down["event"] == "degraded"
    assert module.health_transition({"health": "degraded", "signature": down["signature"]}, degraded) is None
    recovered = module.health_transition({"health": "degraded", "signature": down["signature"]}, healthy)
    assert recovered and recovered["event"] == "recovered"


def test_authorization_actions_do_not_turn_core_health_into_an_outage() -> None:
    module = load_module()
    snapshot = module.health_snapshot({
        "stable": True,
        "startup_ready": True,
        "required_service_failures": [],
        "manual_actions": [{"id": "gmail"}, {"id": "wechat"}],
    })

    assert snapshot["health"] == "healthy"
    assert snapshot["reasons"] == []


def test_health_transitions_stay_internal_by_default(monkeypatch, tmp_path: Path) -> None:
    module = load_module()
    monkeypatch.setattr(module, "HEALTH_TRANSITION_PATH", tmp_path / "health.json")
    monkeypatch.delenv("MARKET_WATCHDOG_SYSTEM_HEALTH_SEND", raising=False)
    calls = []
    monkeypatch.setattr(module, "run_command", lambda *args, **kwargs: calls.append((args, kwargs)) or {"ok": True})

    transition = module.queue_health_transition({
        "stable": False,
        "startup_ready": True,
        "required_service_failures": ["port:wechat_callback"],
    })

    assert transition and transition["event"] == "degraded"
    assert transition["external_notification"] == "suppressed_internal_only"
    assert calls == []


def test_system_runner_invokes_market_alert_dispatcher_after_fusion() -> None:
    text = SCRIPT.read_text(encoding="utf-8")

    assert "def run_market_alert_dispatcher" in text
    assert 'last["market_alerts"] = run_market_alert_dispatcher()' in text


def test_coordinator_keeps_core_symbols_and_rotates_theme_context(monkeypatch, tmp_path: Path) -> None:
    module = load_module()
    policy = tmp_path / "scan_policy.yaml"
    policy.write_text(
        """
tiers:
  core: {symbols: [SPY, QQQ]}
  themes: {symbols: [MU, WDC, GLD, GDX]}
  context: {symbols: [IWM, RSP]}
coordinator:
  core_symbols_each_run: 2
  rotating_theme_symbols_each_run: 2
  maximum_symbols_per_run: 4
""".strip(),
        encoding="utf-8",
    )
    monkeypatch.setattr(module, "SCAN_POLICY_PATH", policy)

    first = module.coordinator_symbols(10, slot=0)
    second = module.coordinator_symbols(10, slot=1)

    assert first[:2] == ["SPY", "QQQ"]
    assert second[:2] == ["SPY", "QQQ"]
    assert first[2:] != second[2:]
    assert len(first) == 4


def test_system_runner_invokes_market_signal_engine_after_layered_scans() -> None:
    text = SCRIPT.read_text(encoding="utf-8")

    assert "def run_market_signal_engine" in text
    assert 'last["market_signals"] = run_market_signal_engine()' in text


def test_proactive_research_rotates_without_price_trigger() -> None:
    module = load_module()
    first = module.proactive_research_plan(0)
    second = module.proactive_research_plan(1)

    assert first["theme"] != second["theme"]
    assert "do not wait for a price anomaly" in first["task"]
    assert "direction" in first["task"]
    assert "broker writes" in first["task"]


def test_daily_brief_due_only_once_after_close() -> None:
    module = load_module()
    et = timezone(timedelta(hours=-4))
    before = datetime(2026, 7, 14, 16, 19, tzinfo=et).astimezone(timezone.utc)
    after = datetime(2026, 7, 14, 16, 20, tzinfo=et).astimezone(timezone.utc)
    policy = {"daily_brief": {"enabled": True, "hour_et": 16, "minute_et": 20, "weekdays_only": True}}

    assert module.daily_brief_due(before, policy, {}) is False
    assert module.daily_brief_due(after, policy, {}) is True
    assert module.daily_brief_due(after, policy, {"last_queued_market_date": "2026-07-14"}) is False


def test_daily_brief_preview_is_once_daily_but_can_be_delivered_after_channel_recovers() -> None:
    module = load_module()
    et = timezone(timedelta(hours=-4))
    after = datetime(2026, 7, 14, 16, 40, tzinfo=et).astimezone(timezone.utc)
    policy = {"daily_brief": {"enabled": True, "hour_et": 16, "minute_et": 40, "weekdays_only": True}}
    state = {"last_generated_market_date": "2026-07-14"}

    assert module.daily_brief_due(after, policy, state, delivery_enabled=False) is False
    assert module.daily_brief_due(after, policy, state, delivery_enabled=True) is True


def test_daily_brief_enqueue_requires_enabled_gmail_channel() -> None:
    module = load_module()

    assert module.daily_brief_enqueue_enabled({"communication_enabled": False}) is False
    assert module.daily_brief_enqueue_enabled({
        "communication_enabled": True,
        "channels": {"gmail": {"enabled": False}},
    }) is False
    assert module.daily_brief_enqueue_enabled({
        "communication_enabled": True,
        "channels": {"gmail": {"enabled": True}},
    }) is True


def test_daily_brief_enqueue_accepts_fresh_auto_ready_gmail_status() -> None:
    module = load_module()
    policy = {
        "communication_enabled": True,
        "channels": {"gmail": {"enabled": False, "auto_enable_when_ready": True}},
    }

    assert module.daily_brief_enqueue_enabled(
        policy,
        channel_status={"channel_activation": {"gmail": False}},
    ) is False
    assert module.daily_brief_enqueue_enabled(
        policy,
        channel_status={"channel_activation": {"gmail": True}},
    ) is True


def test_weekly_brief_runs_friday_after_close_and_catches_up_on_weekend() -> None:
    module = load_module()
    et = timezone(timedelta(hours=-4))
    before = datetime(2026, 7, 17, 17, 9, tzinfo=et).astimezone(timezone.utc)
    after = datetime(2026, 7, 17, 17, 10, tzinfo=et).astimezone(timezone.utc)
    sunday = datetime(2026, 7, 19, 12, 0, tzinfo=et).astimezone(timezone.utc)
    policy = {"weekly_brief": {"enabled": True, "weekday_et": 4, "hour_et": 17, "minute_et": 10}}

    assert module.weekly_brief_due(before, policy, {}) is False
    assert module.weekly_brief_due(after, policy, {}) is True
    assert module.weekly_brief_due(sunday, policy, {}) is True
    week = module.market_week(after)
    assert module.weekly_brief_due(sunday, policy, {"last_queued_week": week}) is False


def test_weekly_preview_can_be_delivered_when_gmail_recovers_in_same_week() -> None:
    module = load_module()
    et = timezone(timedelta(hours=-4))
    sunday = datetime(2026, 7, 19, 12, 0, tzinfo=et).astimezone(timezone.utc)
    policy = {"weekly_brief": {"enabled": True, "weekday_et": 4, "hour_et": 17, "minute_et": 10}}
    state = {"last_generated_week": module.market_week(sunday)}

    assert module.weekly_brief_due(sunday, policy, state, delivery_enabled=False) is False
    assert module.weekly_brief_due(sunday, policy, state, delivery_enabled=True) is True


def test_system_runner_has_outlook_emergency_daily_and_weekly_loops() -> None:
    text = SCRIPT.read_text(encoding="utf-8")
    assert "def run_trend_outlook" in text
    assert "def run_market_emergency_monitor" in text
    assert "def run_daily_market_brief" in text
    assert "def run_weekly_market_brief" in text
    assert "weekly_brief_due" in text
    assert "def run_proactive_news_collector" in text
    assert '"actual_broker_writes"' not in text or "auto_trade_allowed" in text
