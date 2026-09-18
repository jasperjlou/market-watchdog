#!/usr/bin/env python3
"""Read-only market-data router for Moomoo with yfinance fallback.

The router never creates a trade context and never queries account data.  It
normalizes quote source, collection time, and latency class so delayed data
cannot silently masquerade as real-time data.
"""

from __future__ import annotations

import argparse
import importlib
import importlib.util
import json
import math
import os
import socket
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable


APP_DIR = Path(os.environ.get("APP_DIR", "/app"))
CONFIG_PATH = APP_DIR / "config" / "market_data.yaml"
ISOLATED_RUNTIME = APP_DIR / ".runtime" / "market-data"


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def safe_float(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def load_yaml(path: Path = CONFIG_PATH) -> dict[str, Any]:
    try:
        import yaml  # type: ignore
    except Exception:
        return {}
    if not path.exists():
        return {}
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    return payload if isinstance(payload, dict) else {}


def runtime_site_packages(root: Path = ISOLATED_RUNTIME) -> list[Path]:
    candidates = []
    if os.name == "nt":
        candidates.append(root / "Lib" / "site-packages")
    else:
        candidates.extend(sorted((root / "lib").glob("python*/site-packages")))
    return [path for path in candidates if path.is_dir()]


def optional_import(name: str) -> Any:
    try:
        return importlib.import_module(name)
    except Exception as original:
        for path in runtime_site_packages():
            text = str(path)
            if text not in sys.path:
                sys.path.insert(0, text)
        try:
            return importlib.import_module(name)
        except Exception:
            raise original


def module_available(name: str) -> bool:
    if importlib.util.find_spec(name) is not None:
        return True
    for path in runtime_site_packages():
        if (path / name).exists() or any(path.glob(f"{name}-*.dist-info")):
            return True
    return False


def env_value(config: dict[str, Any], key: str, default: Any = None) -> Any:
    env_name = str(config.get(f"{key}_env") or "").strip()
    if env_name and os.environ.get(env_name):
        return os.environ[env_name]
    return config.get(key, default)


def port_open(host: str, port: int, timeout: float) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except Exception:
        return False


def unique_symbols(symbols: list[str]) -> list[str]:
    result: list[str] = []
    for raw in symbols:
        symbol = str(raw).strip().upper()
        if symbol and symbol not in result:
            result.append(symbol)
    return result


def simple_us_equity(symbol: str) -> bool:
    if not symbol or symbol.startswith("^") or "=" in symbol:
        return False
    return all(character.isalnum() or character in {".", "-"} for character in symbol)


def eligible_symbols(provider: str, symbols: list[str], config: dict[str, Any]) -> list[str]:
    exclusions = config.get("symbol_policy", {}).get("provider_exclusions", {}).get(provider, [])
    excluded = {str(value).upper() for value in exclusions}
    if provider in {"moomoo", "ibkr"}:
        return [symbol for symbol in symbols if symbol not in excluded and simple_us_equity(symbol)]
    return [symbol for symbol in symbols if symbol not in excluded]


def frame_rows(frame: Any) -> list[dict[str, Any]]:
    if frame is None or not hasattr(frame, "iterrows"):
        return []
    return [dict(row) for _, row in frame.iterrows()]


def quote_item(
    *,
    symbol: str,
    source: str,
    source_tier: str,
    price: Any,
    collected_at: str,
    provider_update_time: Any = None,
    market_data_type: str,
    delayed: bool | None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    normalized_price = safe_float(price)
    if normalized_price is None or normalized_price <= 0:
        return None
    payload = {
        "symbol": symbol,
        "source": source,
        "source_tier": source_tier,
        "live_price": round(normalized_price, 8),
        "collected_at": collected_at,
        "provider_update_time": str(provider_update_time or ""),
        "market_data_type": market_data_type,
        "delayed": delayed,
        "readonly": True,
    }
    if extra:
        payload.update(extra)
    return payload


def fetch_moomoo(symbols: list[str], provider: dict[str, Any], full_config: dict[str, Any]) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    started_at = utc_now_iso()
    host = str(env_value(provider, "host", "moomoo-opend"))
    port = int(env_value(provider, "port", 11111))
    timeout = float(provider.get("connect_timeout_seconds", 2))
    if not port_open(host, port, timeout):
        return {}, {"ok": False, "reason": "port_unavailable", "host": host, "port": port}
    try:
        api = optional_import(str(provider.get("python_module") or "moomoo"))
    except Exception as exc:
        return {}, {"ok": False, "reason": f"module_unavailable:{exc.__class__.__name__}", "host": host, "port": port}

    if hasattr(api, "SysConfig") and hasattr(api.SysConfig, "enable_console_log"):
        api.SysConfig.enable_console_log(False)
    prefix = str(full_config.get("symbol_policy", {}).get("moomoo_prefix_by_market", {}).get("US", "US."))
    code_to_symbol = {f"{prefix}{symbol}": symbol for symbol in symbols}
    context = None
    try:
        context = api.OpenQuoteContext(host=host, port=port, is_encrypt=None)
        ret, data = context.get_market_snapshot(list(code_to_symbol))
        if ret != getattr(api, "RET_OK", 0):
            return {}, {"ok": False, "reason": "snapshot_request_failed", "host": host, "port": port}
        items: dict[str, dict[str, Any]] = {}
        for row in frame_rows(data):
            code = str(row.get("code") or "")
            symbol = code_to_symbol.get(code)
            if not symbol:
                continue
            item = quote_item(
                symbol=symbol,
                source="moomoo",
                source_tier=str(provider.get("source_tier") or "S1"),
                price=row.get("last_price"),
                collected_at=started_at,
                provider_update_time=row.get("update_time"),
                market_data_type="provider_snapshot_entitlement_dependent",
                delayed=None,
                extra={
                    "open_price": safe_float(row.get("open_price")),
                    "high_price": safe_float(row.get("high_price")),
                    "low_price": safe_float(row.get("low_price")),
                    "previous_close": safe_float(row.get("prev_close_price")),
                    "volume": safe_float(row.get("volume")),
                    "turnover": safe_float(row.get("turnover")),
                    "suspended": bool(row.get("suspension")) if row.get("suspension") is not None else None,
                },
            )
            if item:
                items[symbol] = item
        return items, {"ok": bool(items), "quotes": len(items), "host": host, "port": port, "method": "get_market_snapshot"}
    except Exception as exc:
        return {}, {"ok": False, "reason": exc.__class__.__name__, "host": host, "port": port}
    finally:
        if context is not None:
            try:
                context.close()
            except Exception:
                pass


def ibkr_market_data_type(value: Any) -> tuple[str, bool | None]:
    try:
        code = int(value)
    except (TypeError, ValueError):
        return "unknown", None
    return {
        1: ("realtime", False),
        2: ("frozen", False),
        3: ("delayed", True),
        4: ("delayed_frozen", True),
    }.get(code, ("unknown", None))


def ticker_price(ticker: Any) -> float | None:
    candidates = []
    try:
        candidates.append(ticker.marketPrice())
    except Exception:
        pass
    candidates.extend([getattr(ticker, "last", None), getattr(ticker, "close", None)])
    for value in candidates:
        parsed = safe_float(value)
        if parsed is not None and parsed > 0:
            return parsed
    bid = safe_float(getattr(ticker, "bid", None))
    ask = safe_float(getattr(ticker, "ask", None))
    return (bid + ask) / 2 if bid and ask and bid > 0 and ask > 0 else None


def fetch_ibkr(symbols: list[str], provider: dict[str, Any], _full_config: dict[str, Any]) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    collected_at = utc_now_iso()
    host = str(env_value(provider, "host", "127.0.0.1"))
    port = int(env_value(provider, "port", 4001))
    timeout = float(provider.get("connect_timeout_seconds", 3))
    if not port_open(host, port, min(timeout, 2.0)):
        return {}, {"ok": False, "reason": "port_unavailable", "host": host, "port": port}
    try:
        module = optional_import("ib_insync")
    except Exception as exc:
        return {}, {"ok": False, "reason": f"module_unavailable:{exc.__class__.__name__}", "host": host, "port": port}

    ib = module.IB()
    try:
        client_id = int(provider.get("client_id_base", 980)) + (os.getpid() % 10)
        ib.connect(host, port, clientId=client_id, timeout=timeout, readonly=True)
        ib.reqMarketDataType(int(provider.get("request_market_data_type", 3)))
        contracts = [module.Stock(symbol, "SMART", "USD") for symbol in symbols]
        qualified = list(ib.qualifyContracts(*contracts))
        tickers = ib.reqTickers(*qualified) if qualified else []
        items: dict[str, dict[str, Any]] = {}
        for ticker in tickers:
            contract = getattr(ticker, "contract", None)
            symbol = str(getattr(contract, "symbol", "") or "").upper()
            if symbol not in symbols:
                continue
            kind, delayed = ibkr_market_data_type(getattr(ticker, "marketDataType", None))
            item = quote_item(
                symbol=symbol,
                source="ibkr",
                source_tier=str(provider.get("source_tier") or "S1"),
                price=ticker_price(ticker),
                collected_at=collected_at,
                provider_update_time=getattr(ticker, "time", None),
                market_data_type=kind,
                delayed=delayed,
                extra={
                    "bid": safe_float(getattr(ticker, "bid", None)),
                    "ask": safe_float(getattr(ticker, "ask", None)),
                    "previous_close": safe_float(getattr(ticker, "close", None)),
                    "volume": safe_float(getattr(ticker, "volume", None)),
                },
            )
            if item:
                items[symbol] = item
        return items, {"ok": bool(items), "quotes": len(items), "host": host, "port": port, "readonly": True}
    except Exception as exc:
        return {}, {"ok": False, "reason": exc.__class__.__name__, "host": host, "port": port, "readonly": True}
    finally:
        try:
            ib.disconnect()
        except Exception:
            pass


def select_symbol_frame(data: Any, symbol: str, symbol_count: int) -> Any:
    columns = getattr(data, "columns", None)
    if columns is None:
        return None
    nlevels = getattr(columns, "nlevels", 1)
    if nlevels == 1:
        return data if symbol_count == 1 else None
    level0 = {str(value) for value in columns.get_level_values(0)}
    level1 = {str(value) for value in columns.get_level_values(1)}
    if symbol in level0:
        return data[symbol]
    if symbol in level1:
        return data.xs(symbol, axis=1, level=1)
    return None


def last_series_value(frame: Any, key: str) -> Any:
    try:
        series = frame[key].dropna()
        return series.iloc[-1] if len(series) else None
    except Exception:
        return None


def fetch_yfinance(symbols: list[str], provider: dict[str, Any], _full_config: dict[str, Any]) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    collected_at = utc_now_iso()
    try:
        yf = optional_import("yfinance")
    except Exception as exc:
        return {}, {"ok": False, "reason": f"module_unavailable:{exc.__class__.__name__}"}
    try:
        data = yf.download(
            tickers=symbols,
            period="1d",
            interval="1m",
            prepost=True,
            group_by="ticker",
            auto_adjust=False,
            threads=True,
            progress=False,
        )
        items: dict[str, dict[str, Any]] = {}
        for symbol in symbols:
            frame = select_symbol_frame(data, symbol, len(symbols))
            if frame is None:
                continue
            item = quote_item(
                symbol=symbol,
                source="yfinance",
                source_tier=str(provider.get("source_tier") or "S2"),
                price=last_series_value(frame, "Close"),
                collected_at=collected_at,
                provider_update_time=getattr(frame, "index", [""])[-1] if len(getattr(frame, "index", [])) else "",
                market_data_type=str(provider.get("latency_class") or "near_realtime_aggregator"),
                delayed=None,
                extra={"volume": safe_float(last_series_value(frame, "Volume"))},
            )
            if item:
                items[symbol] = item
        return items, {"ok": bool(items), "quotes": len(items), "method": "batch_1m"}
    except Exception as exc:
        return {}, {"ok": False, "reason": exc.__class__.__name__}


PROVIDER_FETCHERS: dict[str, Callable[[list[str], dict[str, Any], dict[str, Any]], tuple[dict[str, dict[str, Any]], dict[str, Any]]]] = {
    "moomoo": fetch_moomoo,
    "ibkr": fetch_ibkr,
    "yfinance": fetch_yfinance,
}


def fetch_realtime_snapshots(
    symbols: list[str],
    *,
    config: dict[str, Any] | None = None,
    fetchers: dict[str, Callable[[list[str], dict[str, Any], dict[str, Any]], tuple[dict[str, dict[str, Any]], dict[str, Any]]]] | None = None,
) -> dict[str, Any]:
    config = config or load_yaml()
    fetchers = fetchers or PROVIDER_FETCHERS
    requested = unique_symbols(symbols)
    selected: dict[str, dict[str, Any]] = {}
    provider_status: dict[str, dict[str, Any]] = {}
    priority = config.get("selection", {}).get("priority", ["moomoo", "yfinance"])
    providers = config.get("providers", {})
    for provider_name in priority:
        provider = providers.get(provider_name, {}) if isinstance(providers, dict) else {}
        if not isinstance(provider, dict) or provider.get("enabled") is not True:
            provider_status[str(provider_name)] = {"ok": False, "reason": "disabled"}
            continue
        remaining = [symbol for symbol in requested if symbol not in selected]
        eligible = eligible_symbols(str(provider_name), remaining, config)
        if not eligible:
            provider_status[str(provider_name)] = {"ok": True, "quotes": 0, "reason": "no_eligible_symbols"}
            continue
        limit = max(1, int(provider.get("max_batch_symbols", len(eligible))))
        gathered: dict[str, dict[str, Any]] = {}
        statuses = []
        for start in range(0, len(eligible), limit):
            batch = eligible[start : start + limit]
            items, status = fetchers[str(provider_name)](batch, provider, config)
            gathered.update(items)
            statuses.append(status)
        selected.update({symbol: item for symbol, item in gathered.items() if symbol not in selected})
        provider_status[str(provider_name)] = {
            "ok": any(bool(item.get("ok")) for item in statuses),
            "quotes": len(gathered),
            "batches": len(statuses),
            "details": statuses,
        }
    return {
        "version": "1.0",
        "collected_at": utc_now_iso(),
        "requested_symbols": requested,
        "items": [selected[symbol] for symbol in requested if symbol in selected],
        "unresolved_symbols": [symbol for symbol in requested if symbol not in selected],
        "provider_status": provider_status,
        "readonly": True,
        "actual_broker_writes": False,
    }


def status_payload(config: dict[str, Any]) -> dict[str, Any]:
    providers = config.get("providers", {}) if isinstance(config, dict) else {}
    payload: dict[str, Any] = {"version": "1.0", "checked_at": utc_now_iso(), "providers": {}, "readonly": True}
    configured = config.get("selection", {}).get("priority", ["moomoo", "yfinance"])
    for name in dict.fromkeys(str(value) for value in configured):
        provider = providers.get(name, {}) if isinstance(providers, dict) else {}
        module = str(provider.get("python_module") or ("ib_insync" if name == "ibkr" else name))
        item = {"enabled": provider.get("enabled") is True, "module_present": module_available(module)}
        if name in {"moomoo", "ibkr"}:
            host = str(env_value(provider, "host", "127.0.0.1"))
            port = int(env_value(provider, "port", 0))
            item.update({"host": host, "port": port, "port_open": port_open(host, port, 1.0) if port else False})
        payload["providers"][name] = item
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--symbols", default="")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    config = load_yaml()
    if args.status or not args.symbols.strip():
        payload = status_payload(config)
    else:
        payload = fetch_realtime_snapshots([value for value in args.symbols.split(",") if value.strip()], config=config)
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        providers = payload.get("providers") or payload.get("provider_status") or {}
        print(f"MARKET_DATA_ROUTER readonly=true providers={','.join(providers)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
