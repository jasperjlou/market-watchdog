from __future__ import annotations

import importlib.util
import json
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "proactive_news_collector.py"


def load_module():
    spec = importlib.util.spec_from_file_location("proactive_news_collector", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_direction_and_symbol_detection_are_conservative() -> None:
    module = load_module()
    assert module.infer_direction("Micron raises guidance on strong HBM demand") == "bullish"
    assert module.infer_direction("SK hynix faces export restriction and weak demand") == "bearish"
    assert module.infer_direction("SK hynix schedules investor meeting") == "neutral"
    assert module.detect_symbols("SK hynix and Micron expand HBM", ["SKHY", "MU", "WDC"]) == ["SKHY", "MU"]
    assert module.detect_symbols("NASA studies Titan's atmosphere", ["ITA", "PL"]) == []
    assert module.detect_symbols("ITA wins a defense allocation", ["ITA", "PL"]) == ["ITA"]


def test_theme_relevance_rejects_generic_space_news() -> None:
    module = load_module()
    feed = next(item for item in module.FEEDS if item["theme"] == "space_aerospace")
    assert module.is_material_item("NASA publishes a photo of an earthquake", "", feed) is False
    assert module.is_material_item("NASA awards Rocket Lab a launch contract", "", feed) is True


def test_ambiguous_gold_and_fed_terms_require_market_context() -> None:
    module = load_module()
    gold = next(item for item in module.FEEDS if item["theme"] == "gold")
    broad = next(item for item in module.FEEDS if item["theme"] == "broad_market")
    assert module.is_material_item("Gold Mountain fire expands near Ouray", "", gold) is False
    assert module.is_material_item("Gold prices slide as real yields rise", "", gold) is True
    assert module.is_material_item("Next-Gen Financial Inclusion - Federal Reserve", "", broad) is False
    assert module.is_material_item("Federal Reserve minutes signal rate path", "", broad) is True
    assert module.infer_direction("Gold prices slide from record highs") == "bearish"
    assert module.infer_direction("SK Hynix says demand is enormous") == "bullish"


def test_sec_metadata_stays_neutral_until_document_review() -> None:
    module = load_module()
    payload = {
        "filings": {"recent": {
            "form": ["6-K", "3"],
            "filingDate": ["2026-07-14", "2026-07-14"],
            "accessionNumber": ["0001-26-000001", "0001-26-000002"],
            "primaryDocument": ["report.htm", "ownership.xml"],
        }}
    }
    items = module.sec_items_from_payload("SKHY", 2120882, payload, module.date(2026, 7, 10))
    assert len(items) == 1
    assert items[0]["source_tier"] == "S0"
    assert items[0]["direction"] == "neutral"
    assert items[0]["trade_instruction"] is False


def test_run_collection_writes_normalized_feed_and_dedupes(monkeypatch, tmp_path: Path) -> None:
    module = load_module()
    monkeypatch.setattr(module, "STATE_PATH", tmp_path / "state.json")
    monkeypatch.setattr(module, "OUTPUT_PATH", tmp_path / "latest.json")
    monkeypatch.setattr(module, "EVIDENCE_INDEX_PATH", tmp_path / "evidence.json")
    monkeypatch.setattr(module, "RUNS_DIR", tmp_path / "runs")
    now = module.datetime.now(module.timezone.utc)
    gdelt_stamp = now.strftime("%Y%m%dT%H%M%SZ")
    rss_stamp = now.strftime("%a, %d %b %Y %H:%M:%S GMT")

    def fake_json(url: str, **kwargs):
        if "gdeltproject" in url:
            return {"articles": [{"title": "SK hynix raises HBM capacity plan", "url": "https://reuters.com/example", "domain": "reuters.com", "seendate": gdelt_stamp}]}
        return {"filings": {"recent": {"form": [], "filingDate": [], "accessionNumber": [], "primaryDocument": []}}}

    rss = f"""<?xml version='1.0'?><rss><channel><item><title>Micron memory demand update - CNBC</title><link>https://news.google.com/a</link><pubDate>{rss_stamp}</pubDate><source>CNBC</source></item></channel></rss>"""
    first = module.run_collection(queries_per_run=4, max_records=3, lookback_days=7, include_sec=True, json_fetch=fake_json, text_fetch=lambda _url: rss)
    second = module.run_collection(queries_per_run=4, max_records=3, lookback_days=7, include_sec=True, json_fetch=fake_json, text_fetch=lambda _url: rss)

    assert first["new_count"] >= 2
    evidence_path = Path(first["run_dir"]) / "raw_output.txt"
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    assert evidence["worker"] == "manual"
    assert all(item["trade_instruction"] is False for item in evidence["items"])
    assert second["new_count"] == 0
    compact = json.loads((tmp_path / "evidence.json").read_text(encoding="utf-8"))
    assert compact["item_count"] >= 2
    assert all(set(item) <= set(module.EVIDENCE_FIELDS) for item in compact["items"])


def test_gdelt_failure_enters_backoff_and_only_fails_once(monkeypatch, tmp_path: Path) -> None:
    module = load_module()
    monkeypatch.setattr(module, "STATE_PATH", tmp_path / "state.json")
    monkeypatch.setattr(module, "OUTPUT_PATH", tmp_path / "latest.json")
    monkeypatch.setattr(module, "EVIDENCE_INDEX_PATH", tmp_path / "evidence.json")
    monkeypatch.setattr(module, "RUNS_DIR", tmp_path / "runs")
    calls = {"gdelt": 0}

    def fake_json(url: str, **kwargs):
        if "gdeltproject" in url:
            calls["gdelt"] += 1
            raise TimeoutError("provider unavailable")
        return {"filings": {"recent": {"form": [], "filingDate": [], "accessionNumber": [], "primaryDocument": []}}}

    rss = """<?xml version='1.0'?><rss><channel><item><title>Micron HBM demand update - CNBC</title><link>https://news.google.com/a</link><pubDate>Tue, 14 Jul 2026 18:00:00 GMT</pubDate><source>CNBC</source></item></channel></rss>"""
    first = module.run_collection(queries_per_run=4, max_records=3, lookback_days=7, include_sec=False, json_fetch=fake_json, text_fetch=lambda _url: rss)
    second = module.run_collection(queries_per_run=4, max_records=3, lookback_days=7, include_sec=False, json_fetch=fake_json, text_fetch=lambda _url: rss)

    assert calls["gdelt"] == 1
    assert len([error for error in first["errors"] if error.startswith("GDELT:")]) == 1
    assert first["provider_status"]["gdelt"].startswith("backoff_until:")
    assert second["errors"] == []


def test_compact_evidence_prunes_old_and_deduplicates() -> None:
    module = load_module()
    now = module.datetime(2026, 7, 14, 20, 0, tzinfo=module.timezone.utc)
    recent = {"title": "Micron HBM update", "url": "https://example.com/a", "published_at": "2026-07-14T19:00:00Z", "symbols": ["MU"], "extra_raw": "drop"}
    duplicate = {**recent, "summary": "newer normalized summary", "collected_at": "2026-07-14T19:30:00Z"}
    old = {"title": "old", "url": "https://example.com/old", "published_at": "2026-07-01T00:00:00Z"}

    items = module.compact_evidence([recent, old], [duplicate], 7, now=now)

    assert len(items) == 1
    assert items[0]["url"] == "https://example.com/a"
    assert "extra_raw" not in items[0]


def test_compact_evidence_unions_entities_across_theme_duplicates() -> None:
    module = load_module()
    now = module.datetime(2026, 7, 14, 20, 0, tzinfo=module.timezone.utc)
    memory = {
        "title": "SK Hynix rises in Nasdaq debut", "url": "https://example.com/skhy",
        "published_at": "2026-07-14T19:00:00Z", "symbols": ["SKHY"],
        "topics": ["memory_storage"], "direction": "bullish", "source_tier": "S1",
    }
    broad_duplicate = {
        **memory, "symbols": [], "topics": ["broad_market"], "direction": "neutral", "source_tier": "S3",
    }

    items = module.compact_evidence([memory], [broad_duplicate], 7, now=now)

    assert items[0]["symbols"] == ["SKHY"]
    assert items[0]["topics"] == ["memory_storage", "broad_market"]
    assert items[0]["direction"] == "bullish"
    assert items[0]["source_tier"] == "S1"
