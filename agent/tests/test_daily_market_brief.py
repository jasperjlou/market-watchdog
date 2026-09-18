from __future__ import annotations

import importlib.util
import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "daily_market_brief.py"


def load_module():
    spec = importlib.util.spec_from_file_location("daily_market_brief", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def outlook() -> dict:
    return {
        "items": [
            {"symbol": "MU", "category": "stable_up", "risk_score": 20, "confidence_label": "高", "conclusion": "结构偏多", "short_term": {"direction": "偏多"}, "swing": {"direction": "偏多"}, "portfolio": {"review": "none"}},
            {"symbol": "SKHY", "category": "insufficient_history", "risk_score": 55, "confidence_label": "低", "conclusion": "新上市样本不足", "short_term": {"direction": "震荡"}, "swing": {"direction": "震荡"}, "portfolio": {"review": "none"}},
            {"symbol": "RKLB", "category": "watch_down", "risk_score": 61, "confidence_label": "中", "conclusion": "下行风险增强", "short_term": {"direction": "偏空"}, "swing": {"direction": "震荡"}, "portfolio": {"review": "loss_review", "unrealized_pct": -9.2}},
        ],
        "themes": [{"theme": "memory_storage", "label": "偏强", "median_return_5d_pct": 3.1}],
        "news_digest": [{"source_tier": "S0", "direction": "bullish", "title_zh": "存储厂商更新产能计划"}],
    }


def test_daily_brief_is_compact_and_decision_focused() -> None:
    module = load_module()
    subject, body = module.render_brief(
        outlook(),
        {"daily_brief": {"max_symbols_per_section": 3, "max_news_items": 2, "max_body_chars": 800}},
        now=datetime(2026, 7, 14, 21, 0, tzinfo=timezone.utc),
    )

    assert "趋势与风险日报" in subject
    assert "今日回顾" in body
    assert "明日展望" in body
    assert "未来展望（2-6周）" in body
    assert "风险与操作建议" in body
    assert "RKLB" in body
    assert "SKHY" in body
    assert "存储厂商更新产能计划" in body
    assert "平稳上涨：MU" not in body
    assert "已验证消息：" not in body
    assert "Official memory capacity update" not in body
    assert "系统不自动下单" in body
    assert len(body) <= 800
    assert len(body.splitlines()) <= 20


def test_daily_brief_does_not_expand_the_full_watchlist() -> None:
    module = load_module()
    data = outlook()
    data["items"] = [
        {
            "symbol": f"S{index:02d}",
            "category": "range_stable",
            "risk_score": 10,
            "confidence_label": "高",
            "conclusion": "震荡",
            "short_term": {"direction": "震荡"},
            "swing": {"direction": "震荡"},
            "portfolio": {"review": "none"},
        }
        for index in range(69)
    ]
    data["items"].append({
        "symbol": "ASTS", "category": "high_risk", "risk_score": 75,
        "confidence_label": "高", "conclusion": "波动风险上升",
        "short_term": {"direction": "偏空"}, "swing": {"direction": "偏空"},
        "portfolio": {"review": "none"},
    })

    _, body = module.render_brief(
        data,
        {"daily_brief": {"max_symbols_per_section": 4, "max_news_items": 2, "max_body_chars": 900}},
        now=datetime(2026, 7, 14, 21, 0, tzinfo=timezone.utc),
    )

    assert "共70只" in body
    assert "ASTS" in body
    assert "S00, S01" not in body
    assert len(body) <= 900


def test_daily_brief_turns_structured_high_trust_news_into_concise_chinese() -> None:
    module = load_module()
    data = outlook()
    data["news_digest"] = [{
        "source_tier": "S0", "direction": "bearish",
        "title": "Issuer cuts memory guidance", "summary": "English only",
        "entities": ["MU"], "topics": ["memory_storage"], "impact_horizon": "2_6w",
    }]

    _, body = module.render_brief(
        data,
        {"daily_brief": {"max_symbols_per_section": 3, "max_news_items": 2, "max_body_chars": 900}},
        now=datetime(2026, 7, 14, 21, 0, tzinfo=timezone.utc),
    )

    assert "消息：存储（MU）出现官方偏空消息，影响偏中期" in body
    assert "Issuer cuts" not in body


def test_daily_brief_merges_conflicting_generic_news_for_the_same_theme() -> None:
    module = load_module()
    data = outlook()
    data["news_digest"] = [
        {
            "source_tier": "S1", "direction": "bullish", "title": "Gold demand rises",
            "summary": "English only", "entities": ["GLD"], "topics": ["gold"],
            "impact_horizon": "1_5d",
        },
        {
            "source_tier": "S1", "direction": "bearish", "title": "Gold faces yield pressure",
            "summary": "English only", "entities": ["GDX"], "topics": ["gold"],
            "impact_horizon": "1_5d",
        },
    ]

    _, body = module.render_brief(
        data,
        {"daily_brief": {"max_symbols_per_section": 3, "max_news_items": 2, "max_body_chars": 900}},
        now=datetime(2026, 7, 14, 21, 0, tzinfo=timezone.utc),
    )

    assert "消息：黄金（GLD、GDX）高可信消息多空分化，短线方向需等待价格确认。" in body
    assert body.count("消息：黄金") == 1


def test_daily_brief_uses_gmail_and_preserves_wechat_quota(monkeypatch) -> None:
    module = load_module()
    commands = []

    def fake_run(command, **kwargs):
        commands.append(command)
        return SimpleNamespace(returncode=0, stdout="queued")

    monkeypatch.setattr(module.subprocess, "run", fake_run)
    result = module.enqueue_via_gateway("日报", "正文")

    assert result["ok"] is True
    command = commands[0]
    assert command[command.index("--channel") + 1] == "gmail"


def test_daily_brief_deduplicates_same_market_day(monkeypatch, tmp_path: Path) -> None:
    module = load_module()
    outlook_path = tmp_path / "outlook.json"
    state_path = tmp_path / "state.json"
    policy_path = tmp_path / "policy.yaml"
    outlook_path.write_text(json.dumps(outlook()), encoding="utf-8")
    policy_path.write_text("daily_brief:\n  max_symbols_per_section: 3\n", encoding="utf-8")
    monkeypatch.setattr(module, "REPORTS_DIR", tmp_path / "reports")
    sent = []

    def sender(subject: str, body: str) -> dict:
        sent.append((subject, body))
        return {"ok": True}

    now = datetime(2026, 7, 14, 21, 0, tzinfo=timezone.utc)
    first = module.run_daily_brief(outlook_path, state_path, policy_path, enqueue=True, force=False, now=now, sender=sender)
    second = module.run_daily_brief(outlook_path, state_path, policy_path, enqueue=True, force=False, now=now, sender=sender)

    assert first["ok"] is True
    assert second["skipped"] is True
    assert len(sent) == 1


def test_daily_brief_preview_records_generation_without_claiming_delivery(monkeypatch, tmp_path: Path) -> None:
    module = load_module()
    outlook_path = tmp_path / "outlook.json"
    state_path = tmp_path / "state.json"
    policy_path = tmp_path / "policy.yaml"
    outlook_path.write_text(json.dumps(outlook()), encoding="utf-8")
    policy_path.write_text("daily_brief:\n  max_symbols_per_section: 3\n", encoding="utf-8")
    monkeypatch.setattr(module, "REPORTS_DIR", tmp_path / "reports")

    result = module.run_daily_brief(
        outlook_path,
        state_path,
        policy_path,
        enqueue=False,
        force=False,
        now=datetime(2026, 7, 14, 21, 0, tzinfo=timezone.utc),
    )

    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert result["ok"] is True
    assert state["last_generated_market_date"] == "2026-07-14"
    assert state["last_delivery_status"] == "preview_only"
    assert "last_queued_market_date" not in state


def test_weekly_brief_is_compact_and_focuses_on_next_week() -> None:
    module = load_module()
    data = outlook()
    data["items"][0]["return_5d_pct"] = 6.2
    data["items"][0]["return_20d_pct"] = 12.1
    data["items"][1]["return_5d_pct"] = 1.0
    data["items"][2]["return_5d_pct"] = -7.4

    subject, body = module.render_weekly_brief(
        data,
        {"weekly_brief": {"max_symbols_per_section": 4, "max_news_items": 2, "max_body_chars": 1100}},
        now=datetime(2026, 7, 17, 22, 0, tzinfo=timezone.utc),
    )

    assert "周度回顾与下周展望" in subject
    assert "本周回顾" in body
    assert "相对较强：MU（5日+6.2%）" in body
    assert "相对较弱：RKLB（5日-7.4%）" in body
    assert "下周关注" in body
    assert "未来展望（2-6周）" in body
    assert "系统不自动下单" in body
    assert len(body) <= 1100


def test_weekly_brief_uses_gmail_and_weekly_kind(monkeypatch) -> None:
    module = load_module()
    commands = []

    def fake_run(command, **kwargs):
        commands.append(command)
        return SimpleNamespace(returncode=0, stdout="queued")

    monkeypatch.setattr(module.subprocess, "run", fake_run)
    result = module.enqueue_weekly_via_gateway("周报", "正文")

    assert result["ok"] is True
    command = commands[0]
    assert command[command.index("--channel") + 1] == "gmail"
    assert command[command.index("--kind") + 1] == "weekly_brief"


def test_weekly_brief_deduplicates_same_market_week(monkeypatch, tmp_path: Path) -> None:
    module = load_module()
    outlook_path = tmp_path / "outlook.json"
    state_path = tmp_path / "state.json"
    policy_path = tmp_path / "policy.yaml"
    outlook_path.write_text(json.dumps(outlook()), encoding="utf-8")
    policy_path.write_text("weekly_brief:\n  max_symbols_per_section: 4\n", encoding="utf-8")
    monkeypatch.setattr(module, "REPORTS_DIR", tmp_path / "reports")
    sent = []

    def sender(subject: str, body: str) -> dict:
        sent.append((subject, body))
        return {"ok": True}

    friday = datetime(2026, 7, 17, 22, 0, tzinfo=timezone.utc)
    sunday = datetime(2026, 7, 19, 20, 0, tzinfo=timezone.utc)
    first = module.run_weekly_brief(
        outlook_path, state_path, policy_path, enqueue=True, force=False, now=friday, sender=sender,
    )
    second = module.run_weekly_brief(
        outlook_path, state_path, policy_path, enqueue=True, force=False, now=sunday, sender=sender,
    )

    assert first["ok"] is True
    assert second["skipped"] is True
    assert len(sent) == 1
    assert json.loads(state_path.read_text(encoding="utf-8"))["last_queued_week"] == first["market_week"]
