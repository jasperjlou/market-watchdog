from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


AGENT_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = AGENT_ROOT / "scripts"
SCRIPT = SCRIPTS / "kline_snapshot.py"


def load_module():
    sys.path.insert(0, str(SCRIPTS))
    try:
        spec = importlib.util.spec_from_file_location("kline_snapshot_test", SCRIPT)
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path.remove(str(SCRIPTS))


class FakeSeries:
    def __init__(self, values):
        self.values = values

    def dropna(self):
        return self

    def tolist(self):
        return list(self.values)


class FakeFrame:
    def __init__(self):
        closes = [100 + index for index in range(70)]
        self.values = {
            "Close": closes,
            "High": [value + 1 for value in closes],
            "Low": [value - 1 for value in closes],
            "Volume": [1000 + index * 10 for index in range(70)],
        }

    def __getitem__(self, name):
        return FakeSeries(self.values[name])


def test_live_quote_overlay_keeps_provider_and_latency_metadata() -> None:
    module = load_module()
    live = {
        "source": "ibkr",
        "source_tier": "S1",
        "live_price": 175,
        "previous_close": 170,
        "market_data_type": "delayed",
        "delayed": True,
        "provider_update_time": "2026-07-14T14:00:00Z",
        "collected_at": "2026-07-14T14:00:02Z",
    }

    item = module.build_features("AAPL", FakeFrame(), live, "themes", "ignored")

    assert item["source"] == "ibkr"
    assert item["history_source"] == "yfinance"
    assert item["market_data_type"] == "delayed"
    assert item["delayed"] is True
    assert item["last_close"] == 175
    assert item["return_1d_pct"] == 2.9412


def test_layered_merge_replaces_same_symbol_but_keeps_other_tiers() -> None:
    module = load_module()
    existing = {"items": [{"symbol": "SPY", "scan_tier": "core", "last_close": 600}], "tier_status": {}}
    current = {
        "scan_tier": "themes",
        "run_at": "now",
        "items": [{"symbol": "MU", "scan_tier": "themes", "last_close": 200}],
        "failures": [],
        "provider_status": {},
    }

    merged = module.merge_payload(existing, current)

    assert [item["symbol"] for item in merged["items"]] == ["MU", "SPY"]
    assert merged["tier_status"]["themes"]["symbols"] == 1
    assert merged["actual_broker_writes"] is False


def test_history_batch_retries_only_missing_symbols(monkeypatch) -> None:
    module = load_module()

    class Columns:
        nlevels = 1

    class DownloadFrame(FakeFrame):
        columns = Columns()

    empty = DownloadFrame()
    empty.values["Close"] = []
    good = DownloadFrame()

    class FakeYFinance:
        def __init__(self):
            self.calls = []

        def download(self, *, tickers, **_kwargs):
            self.calls.append(tickers)
            return empty if len(self.calls) == 1 else good

    fake = FakeYFinance()
    monkeypatch.setattr(module, "optional_import", lambda _name: fake)
    monkeypatch.setattr(module, "select_symbol_frame", lambda data, _symbol, _count: data)
    monkeypatch.setattr(module.time, "sleep", lambda _seconds: None)

    frames = module.fetch_history_frames(["SMH"], "6mo", "1d")

    assert len(fake.calls) == 2
    assert len(module.clean_series(frames["SMH"], "Close")) >= 3
