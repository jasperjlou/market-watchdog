from __future__ import annotations

import importlib.util
from pathlib import Path


AGENT_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = AGENT_ROOT / "scripts" / "market_alert_dispatcher.py"


def load_module():
    assert SCRIPT.exists(), "market alert dispatcher is not implemented"
    spec = importlib.util.spec_from_file_location("market_alert_dispatcher", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_selects_only_new_l2_or_higher_market_events() -> None:
    module = load_module()
    draft = {
        "items": [
            {"event_id": "e1", "alert_level": "L1", "title": "low"},
            {"event_id": "e2", "alert_level": "L2", "title": "medium"},
            {"event_id": "e3", "alert_level": "L3", "title": "already sent"},
            {"event_id": "e4", "alert_level": "L4", "title": "portfolio hit"},
        ]
    }
    state = {"queued": {"e3:L3": "2026-07-13T00:00:00Z"}}

    selected = module.select_new_alerts(draft, state, min_level="L2")

    assert [item["event_id"] for item in selected] == ["e2", "e4"]


def test_alert_payload_is_concise_and_never_authorizes_broker_writes() -> None:
    module = load_module()
    item = {
        "event_id": "e4",
        "alert_level": "L4",
        "title": "Portfolio event",
        "entities": ["NVDA", "TSM"],
        "raw_summary": "Official event with price resonance.",
        "trade_recommendation": "review hedge",
    }

    alert = module.alert_payload(item)

    assert alert["channel"] == "telegram"
    assert alert["priority"] == "urgent"
    assert "L4" in alert["subject"]
    lines = alert["body"].splitlines()
    assert len(lines) == 3
    assert lines[0].startswith("相关事件说明：")
    assert lines[1].startswith("价格趋势：")
    assert lines[2].startswith("操作建议：")
    assert len(alert["body"]) <= 220
    assert "placeOrder" not in alert["body"]


def test_successful_enqueue_is_deduplicated_on_next_pass(tmp_path: Path) -> None:
    module = load_module()
    draft_path = tmp_path / "draft.json"
    state_path = tmp_path / "state.json"
    draft_path.write_text(
        '{"items":[{"event_id":"e2","alert_level":"L2","title":"new event","entities":["AMD"],'
        '"ai_review_status":"complete","raw_summary":"Two workers and Codex reviewed the event.",'
        '"trade_recommendation":"Wait for confirmation and protect existing exposure.",'
        '"invalidation":"Price loses the confirmed support level.",'
        '"next_check":"Next 15 minute snapshot and official filing check."}]}',
        encoding="utf-8",
    )
    calls = []

    def enqueue(alert):
        calls.append(alert)
        return {"ok": True}

    first = module.dispatch_once(draft_path, state_path, min_level="L2", enqueue=enqueue)
    second = module.dispatch_once(draft_path, state_path, min_level="L2", enqueue=enqueue)

    assert first["queued_count"] == 1
    assert second["queued_count"] == 0
    assert len(calls) == 1


def test_incomplete_fusion_event_is_deferred_instead_of_sending_bare_alert(tmp_path: Path) -> None:
    module = load_module()
    draft_path = tmp_path / "draft.json"
    state_path = tmp_path / "state.json"
    draft_path.write_text(
        '{"items":[{"event_id":"bare","alert_level":"L2","title":"new event","entities":["AMD"]}]}',
        encoding="utf-8",
    )
    calls = []

    result = module.dispatch_once(
        draft_path, state_path, min_level="L2",
        enqueue=lambda alert: calls.append(alert) or {"ok": True},
    )

    assert result["queued_count"] == 0
    assert result["deferred_count"] == 1
    assert calls == []
