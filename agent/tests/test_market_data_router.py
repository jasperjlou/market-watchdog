from __future__ import annotations

import importlib.util
from pathlib import Path


AGENT_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = AGENT_ROOT / "scripts" / "market_data_router.py"


def load_module():
    spec = importlib.util.spec_from_file_location("market_data_router_test", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_provider_router_uses_first_successful_source_per_symbol() -> None:
    module = load_module()
    config = {
        "selection": {"priority": ["moomoo", "ibkr", "yfinance"]},
        "providers": {
            "moomoo": {"enabled": True, "max_batch_symbols": 400},
            "ibkr": {"enabled": True, "max_batch_symbols": 100},
            "yfinance": {"enabled": True, "max_batch_symbols": 100},
        },
        "symbol_policy": {"provider_exclusions": {}},
    }

    def item(symbol: str, source: str) -> dict:
        return {"symbol": symbol, "source": source, "live_price": 100, "readonly": True}

    fetchers = {
        "moomoo": lambda symbols, *_: ({"AAPL": item("AAPL", "moomoo")}, {"ok": True}),
        "ibkr": lambda symbols, *_: ({"MSFT": item("MSFT", "ibkr")}, {"ok": True}),
        "yfinance": lambda symbols, *_: ({symbol: item(symbol, "yfinance") for symbol in symbols}, {"ok": True}),
    }

    result = module.fetch_realtime_snapshots(["AAPL", "MSFT", "^VIX"], config=config, fetchers=fetchers)
    by_symbol = {item["symbol"]: item for item in result["items"]}

    assert by_symbol["AAPL"]["source"] == "moomoo"
    assert by_symbol["MSFT"]["source"] == "ibkr"
    assert by_symbol["^VIX"]["source"] == "yfinance"
    assert result["actual_broker_writes"] is False


def test_ibkr_market_data_type_preserves_delay_semantics() -> None:
    module = load_module()

    assert module.ibkr_market_data_type(1) == ("realtime", False)
    assert module.ibkr_market_data_type(3) == ("delayed", True)
    assert module.ibkr_market_data_type(4) == ("delayed_frozen", True)
    assert module.ibkr_market_data_type("bad") == ("unknown", None)


def test_broker_quote_providers_exclude_indices_futures_and_fx() -> None:
    module = load_module()
    symbols = ["AAPL", "BRK.B", "^VIX", "GC=F", "USDJPY=X"]

    assert module.eligible_symbols("moomoo", symbols, {"symbol_policy": {"provider_exclusions": {}}}) == ["AAPL", "BRK.B"]
    assert module.eligible_symbols("ibkr", symbols, {"symbol_policy": {"provider_exclusions": {}}}) == ["AAPL", "BRK.B"]
