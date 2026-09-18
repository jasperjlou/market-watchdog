from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path


AGENT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AGENT_DIR))

from scripts import codex_fusion_gate as fusion


NOW = datetime(2026, 7, 13, 1, 0, tzinfo=timezone.utc)


def evidence(
    *,
    url: str,
    tier: str = "S0",
    published_at: str = "2026-07-13T00:55:00Z",
    source_name: str = "official-source",
    contradiction_status: str = "",
) -> dict:
    return {
        "title": "Material AAPL event",
        "url": url,
        "source_id": source_name,
        "source_name": source_name,
        "source_tier": tier,
        "published_at": published_at,
        "symbols": ["AAPL"],
        "topics": ["earnings", "guidance"],
        "summary": "Guidance changed with material upside and downside risk.",
        "confidence": "high",
        "contradiction_status": contradiction_status,
    }


def kline(*, collected_at: str = "2026-07-13T00:58:00Z") -> dict:
    return {
        "AAPL": {
            "symbol": "AAPL",
            "price_resonance": True,
            "trend_label": "uptrend",
            "collected_at": collected_at,
        }
    }


def normalize(item: dict, all_items: list[dict], *, holdings: set[str] | None = None) -> dict:
    return fusion.normalize_event(
        item,
        kline(),
        fusion.corroboration_counts(all_items),
        holdings or set(),
        set(),
        {"locks": {}, "risk": {"status": "active"}},
        now=NOW,
    )


def test_duplicate_url_does_not_count_as_independent_corroboration() -> None:
    first = evidence(url="https://issuer.example/release", source_name="issuer")
    duplicate = evidence(url="https://issuer.example/release", source_name="news-mirror")

    result = normalize(first, [first, duplicate])

    assert result["independent_source_count"] == 1
    assert result["official_source_count"] == 1
    assert result["has_dual_official"] is False


def test_tracking_query_and_reordered_topics_do_not_create_new_evidence() -> None:
    first = evidence(url="https://issuer.example/release?utm_source=worker-a", source_name="issuer")
    duplicate = evidence(url="https://issuer.example/release?ref=worker-b", source_name="mirror")
    duplicate["topics"] = ["guidance", "earnings"]

    result = normalize(first, [first, duplicate])

    assert result["independent_source_count"] == 1
    assert result["official_source_count"] == 1


def test_two_unique_official_sources_enable_dual_official_confirmation() -> None:
    issuer = evidence(url="https://issuer.example/release", source_name="issuer", tier="S0")
    regulator = evidence(url="https://regulator.example/notice", source_name="regulator", tier="S0")

    result = normalize(issuer, [issuer, regulator])

    assert result["independent_source_count"] == 2
    assert result["official_source_count"] == 2
    assert result["has_dual_official"] is True


def test_stale_holding_hit_cannot_become_l4_or_create_order_draft() -> None:
    stale = evidence(
        url="https://issuer.example/old-release",
        source_name="issuer",
        published_at="2026-07-09T00:00:00Z",
    )
    confirmation = evidence(
        url="https://regulator.example/old-notice",
        source_name="regulator",
        published_at="2026-07-09T00:05:00Z",
    )

    result = normalize(stale, [stale, confirmation], holdings={"AAPL"})

    assert result["freshness_status"] == "stale"
    assert result["alert_level"] != "L4"
    assert result["order_ticket_draft_allowed"] is False
    assert "stale_evidence" in result["decision_reasons"]


def test_fresh_official_price_resonance_holding_hit_can_reach_l4() -> None:
    issuer = evidence(url="https://issuer.example/release", source_name="issuer")
    regulator = evidence(url="https://regulator.example/notice", source_name="regulator")

    result = normalize(issuer, [issuer, regulator], holdings={"AAPL"})

    assert result["freshness_status"] == "fresh"
    assert result["has_fresh_market_context"] is True
    assert result["alert_level"] == "L4"
    assert result["order_ticket_draft_allowed"] is True
    assert result["decision_engine_version"] == "0.4"


def test_stale_market_context_blocks_order_draft_even_with_fresh_evidence() -> None:
    issuer = evidence(url="https://issuer.example/release", source_name="issuer")
    regulator = evidence(url="https://regulator.example/notice", source_name="regulator")
    result = fusion.normalize_event(
        issuer,
        kline(collected_at="2026-07-12T20:00:00Z"),
        fusion.corroboration_counts([issuer, regulator]),
        {"AAPL"},
        set(),
        {"locks": {}, "risk": {"status": "active"}},
        now=NOW,
    )

    assert result["has_fresh_market_context"] is False
    assert result["alert_level"] != "L4"
    assert result["order_ticket_draft_allowed"] is False
    assert "stale_or_missing_market_context" in result["decision_reasons"]


def test_unresolved_contradiction_caps_level_and_blocks_order_draft() -> None:
    disputed = evidence(
        url="https://issuer.example/disputed",
        source_name="issuer",
        contradiction_status="unresolved",
    )
    confirmations = [
        disputed,
        evidence(url="https://regulator.example/notice", source_name="regulator"),
        evidence(url="https://exchange.example/notice", source_name="exchange"),
    ]

    result = normalize(disputed, confirmations, holdings={"AAPL"})

    assert result["has_unresolved_contradiction"] is True
    assert result["alert_level"] == "L1"
    assert result["order_ticket_draft_allowed"] is False
    assert "unresolved_contradiction" in result["decision_reasons"]


def test_live_loader_uses_compact_feed_index_and_skips_raw_feed_runs(monkeypatch, tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    normal = runs / "20260714_worker"
    feed = runs / "20260714_feed_space"
    normal.mkdir(parents=True)
    feed.mkdir(parents=True)
    (normal / "raw_output.txt").write_text(json.dumps({"worker": "codex", "items": [evidence(url="https://example.com/worker")]}), encoding="utf-8")
    (feed / "raw_output.txt").write_text(json.dumps({"worker": "manual", "items": [{"title": "Titan rotorcraft", "url": "https://example.com/bad", "symbols": ["ITA"]}]}), encoding="utf-8")
    compact_path = tmp_path / "proactive_news_evidence.json"
    compact_path.write_text(json.dumps({"items": [{"title": "Rocket Lab contract", "url": "https://example.com/good", "symbols": ["RKLB"], "source_tier": "S1"}]}), encoding="utf-8")
    monkeypatch.setattr(fusion, "RUNS_DIR", runs)
    monkeypatch.setattr(fusion, "PROACTIVE_EVIDENCE_PATH", compact_path)

    loaded = fusion.load_worker_items()
    urls = {item["url"] for item in loaded}

    assert "https://example.com/worker" in urls
    assert "https://example.com/good" in urls
    assert "https://example.com/bad" not in urls
