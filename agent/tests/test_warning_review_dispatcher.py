from __future__ import annotations

import importlib.util
import json
from datetime import datetime, timezone
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "warning_review_dispatcher.py"
NOW = datetime(2026, 7, 14, 22, 30, tzinfo=timezone.utc)


def load_module():
    spec = importlib.util.spec_from_file_location("warning_review_dispatcher", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def test_reviewed_warning_sends_one_complete_brief_and_then_deduplicates(tmp_path: Path) -> None:
    module = load_module()
    state_path = tmp_path / "warning_state.json"
    reviews_dir = tmp_path / "reviews"
    outlook_path = tmp_path / "outlook.json"
    portfolio_path = tmp_path / "portfolio.json"
    correlation_id = "warning_mu_1"
    write_json(state_path, {"warning_chamber": {"MU": {
        "symbol": "MU", "status": "research_pending", "revision": 1,
        "correlation_id": correlation_id, "alert_level": "L2", "direction": "up",
        "move_pct": 9.0, "kind": "immediate_extreme", "created_at": "2026-07-14T22:00:00Z",
    }}})
    write_json(reviews_dir / f"{correlation_id}.json", {
        "correlation_id": correlation_id,
        "status": "complete",
        "models": {"google": "ok", "grok": "ok", "codex": "gpt-5.5"},
        "synthesis": {
            "event": "存储板块成交量与价格同步放大，官方原因仍待确认",
            "summary": "价格突破近期区间，但新闻证据尚不足以确认单一催化。",
            "evidence_summary": ["Google 未找到新增官方公告", "Grok 发现行业需求讨论升温但可信度中等"],
            "recommendation": "已有仓位先上移保护位；无仓位不要追高，等待回踩确认。",
            "invalidation": "跌回突破位并连续两个观察点无法收复。",
            "next_check": "下一个 15 分钟快照及公司/SEC 新披露。",
            "tomorrow_outlook": "若量能维持，次日延续概率偏高，但高开回落风险上升。",
            "future_outlook": "2-6 周仍取决于存储价格、库存和指引是否继续改善。",
            "confidence": "medium",
            "citations": ["https://example.com/official", "https://example.com/context"],
        },
    })
    write_json(outlook_path, {"items": [{
        "symbol": "MU", "category": "watch_up", "risk_score": 62, "confidence_label": "中",
        "short_term": {"direction": "偏多"}, "swing": {"direction": "中性偏多"},
        "invalidation": "跌破 SMA20", "portfolio": {"review": "none"},
    }]})
    write_json(portfolio_path, {"positions": []})
    sent: list[dict[str, str]] = []

    first = module.dispatch_ready(
        state_path, reviews_dir, outlook_path, portfolio_path,
        enqueue=lambda alert: sent.append(alert) or {"ok": True}, now=NOW,
    )
    second = module.dispatch_ready(
        state_path, reviews_dir, outlook_path, portfolio_path,
        enqueue=lambda alert: sent.append(alert) or {"ok": True}, now=NOW,
    )

    assert first["queued_count"] == 1
    assert second["queued_count"] == 0
    assert len(sent) == 1
    lines = sent[0]["body"].splitlines()
    assert len(lines) == 3
    assert lines[0].startswith("相关事件说明：")
    assert lines[1].startswith("价格趋势：")
    assert lines[2].startswith("操作建议：")
    assert sent[0]["channel"] == "telegram"
    assert len(sent[0]["body"]) <= 220
    assert "模型" not in sent[0]["body"]
    assert "证据列表" not in sent[0]["body"]
    assert module.load_json(state_path, {})["warning_chamber"]["MU"]["status"] == "active_warning"


def test_incomplete_review_never_emits_a_bare_alert(tmp_path: Path) -> None:
    module = load_module()
    state_path = tmp_path / "warning_state.json"
    reviews_dir = tmp_path / "reviews"
    outlook_path = tmp_path / "outlook.json"
    portfolio_path = tmp_path / "portfolio.json"
    write_json(state_path, {"warning_chamber": {"MU": {
        "symbol": "MU", "status": "research_pending", "revision": 1,
        "correlation_id": "warning_mu_1", "alert_level": "L2", "direction": "up", "move_pct": 9,
    }}})
    write_json(reviews_dir / "warning_mu_1.json", {
        "correlation_id": "warning_mu_1", "status": "partial", "synthesis": {"summary": ""},
    })
    write_json(outlook_path, {"items": []})
    write_json(portfolio_path, {"positions": []})
    sent: list[dict[str, str]] = []

    result = module.dispatch_ready(
        state_path, reviews_dir, outlook_path, portfolio_path,
        enqueue=lambda alert: sent.append(alert) or {"ok": True}, now=NOW,
    )

    assert result["queued_count"] == 0
    assert result["deferred_count"] == 1
    assert sent == []


def test_l2_resolution_is_saved_for_daily_summary_instead_of_sent(tmp_path: Path) -> None:
    module = load_module()
    state_path = tmp_path / "warning_state.json"
    reviews_dir = tmp_path / "reviews"
    outlook_path = tmp_path / "outlook.json"
    portfolio_path = tmp_path / "portfolio.json"
    write_json(state_path, {"warning_chamber": {"MU": {
        "symbol": "MU", "status": "resolution_pending", "revision": 2,
        "alert_level": "L2", "direction": "up", "move_pct": 0.8,
        "last_reviewed_move_pct": 9, "kind": "immediate_extreme",
    }}})
    write_json(outlook_path, {"items": [{
        "symbol": "MU", "risk_score": 30, "short_term": {"direction": "震荡"},
        "swing": {"direction": "中性"},
    }]})
    write_json(portfolio_path, {"positions": []})
    sent = []

    result = module.dispatch_ready(
        state_path, reviews_dir, outlook_path, portfolio_path,
        enqueue=lambda alert: sent.append(alert) or {"ok": True}, now=NOW,
    )
    repeated = module.dispatch_ready(
        state_path, reviews_dir, outlook_path, portfolio_path,
        enqueue=lambda alert: sent.append(alert) or {"ok": True}, now=NOW,
    )

    assert result["queued_count"] == 0
    assert repeated["queued_count"] == 0
    assert sent == []
    assert module.load_json(state_path, {})["warning_chamber"]["MU"]["status"] == "resolved"


def test_l3_resolution_still_sends_one_urgent_update(tmp_path: Path) -> None:
    module = load_module()
    state_path = tmp_path / "warning_state.json"
    reviews_dir = tmp_path / "reviews"
    outlook_path = tmp_path / "outlook.json"
    portfolio_path = tmp_path / "portfolio.json"
    write_json(state_path, {"warning_chamber": {"MU": {
        "symbol": "MU", "status": "resolution_pending", "revision": 2,
        "alert_level": "L3", "direction": "down", "move_pct": 0.7,
        "last_reviewed_move_pct": -14, "kind": "immediate_extreme",
    }}})
    write_json(outlook_path, {"items": [{
        "symbol": "MU", "risk_score": 35, "short_term": {"direction": "震荡"},
        "swing": {"direction": "中性"},
    }]})
    write_json(portfolio_path, {"positions": []})
    sent = []

    result = module.dispatch_ready(
        state_path, reviews_dir, outlook_path, portfolio_path,
        enqueue=lambda alert: sent.append(alert) or {"ok": True}, now=NOW,
    )

    assert result["queued_count"] == 1
    assert sent[0]["channel"] == "telegram"
    assert "解除警戒" in sent[0]["subject"]


def test_low_signal_l2_stays_internal_until_risk_or_move_is_material(tmp_path: Path) -> None:
    module = load_module()
    state_path = tmp_path / "warning_state.json"
    reviews_dir = tmp_path / "reviews"
    outlook_path = tmp_path / "outlook.json"
    portfolio_path = tmp_path / "portfolio.json"
    correlation_id = "warning_gld_1"
    write_json(state_path, {"warning_chamber": {"GLD": {
        "symbol": "GLD", "status": "research_pending", "revision": 1,
        "correlation_id": correlation_id, "alert_level": "L2", "direction": "up",
        "move_pct": 6.4, "kind": "immediate_extreme",
    }}})
    write_json(reviews_dir / f"{correlation_id}.json", {
        "status": "complete",
        "synthesis": {
            "event": "黄金价格快速上涨，暂未发现能够解释全部涨幅的新消息",
            "summary": "短线波动扩大，后续仍需观察现货和基金净值是否同步",
            "recommendation": "有仓位继续观察，无仓位先等价格稳定",
        "invalidation": "价格重新回到原有震荡区间",
        "next_check": "下一批有效行情到达后再检查",
        },
    })
    write_json(outlook_path, {"items": [{
        "symbol": "GLD", "risk_score": 25, "short_term": {"direction": "偏多"},
        "swing": {"direction": "震荡"},
    }]})
    write_json(portfolio_path, {"positions": []})
    sent = []

    result = module.dispatch_ready(
        state_path, reviews_dir, outlook_path, portfolio_path,
        enqueue=lambda alert: sent.append(alert) or {"ok": True}, now=NOW,
    )

    assert result["queued_count"] == 0
    assert sent == []
    warning = module.load_json(state_path, {})["warning_chamber"]["GLD"]
    assert warning["status"] == "active_warning"
    assert warning["user_notification_suppressed"] == "below_external_threshold"


def test_l2_risk_requires_a_minimum_move_while_extreme_move_still_notifies() -> None:
    module = load_module()
    settings = {
        "l2_min_risk_score": 65,
        "l2_min_abs_move_pct": 8,
        "l2_risk_move_floor_pct": 5,
    }
    portfolio = {"positions": []}

    assert module.should_notify(
        {"alert_level": "L2", "symbol": "MU", "move_pct": 5.1},
        {"risk_score": 65, "portfolio": {"review": "none"}}, portfolio, settings,
    ) is True
    assert module.should_notify(
        {"alert_level": "L2", "symbol": "MU", "move_pct": 4.9},
        {"risk_score": 80, "portfolio": {"review": "none"}}, portfolio, settings,
    ) is False
    assert module.should_notify(
        {"alert_level": "L2", "symbol": "MU", "move_pct": 8.0},
        {"risk_score": 10, "portfolio": {"review": "none"}}, portfolio, settings,
    ) is True
