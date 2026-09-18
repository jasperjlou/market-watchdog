#!/usr/bin/env python3
"""Layered K-line features with labelled live-quote provider overlays."""

from __future__ import annotations

import argparse
import json
import math
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean, pstdev
from typing import Any

from market_data_router import fetch_realtime_snapshots, optional_import, safe_float, select_symbol_frame


PROJECT_DIR = Path(__file__).resolve().parents[2]
APP_DIR = Path(os.environ.get("APP_DIR", str(PROJECT_DIR)))
AGENT_DIR = APP_DIR / "agent"
STATE_DIR = AGENT_DIR / "state"
REPORTS_DIR = APP_DIR / "reports"
WATCHLIST_PATH = APP_DIR / "config" / "watchlist.yaml"
SCAN_POLICY_PATH = APP_DIR / "config" / "scan_policy.yaml"


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def load_yaml(path: Path) -> dict[str, Any]:
    try:
        import yaml  # type: ignore
    except Exception:
        return {}
    if not path.exists():
        return {}
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    return payload if isinstance(payload, dict) else {}


def pct_change(current: float | None, previous: float | None) -> float | None:
    if current is None or previous is None or previous == 0:
        return None
    return round(((current - previous) / previous) * 100.0, 4)


def symbols_from_watchlist(max_symbols: int) -> list[str]:
    items = load_yaml(WATCHLIST_PATH).get("items", [])
    symbols: list[str] = []
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict) or item.get("enabled") is not True:
            continue
        symbol = str(item.get("symbol") or "").strip().upper()
        if symbol and symbol not in symbols:
            symbols.append(symbol)
    return symbols[:max_symbols]


def symbols_from_tier(tier: str, max_symbols: int) -> list[str]:
    policy = load_yaml(SCAN_POLICY_PATH)
    config = policy.get("tiers", {}).get(tier, {}) if isinstance(policy, dict) else {}
    symbols = config.get("symbols", []) if isinstance(config, dict) else []
    result = []
    for value in symbols if isinstance(symbols, list) else []:
        symbol = str(value).strip().upper()
        if symbol and symbol not in result:
            result.append(symbol)
    return result[:max_symbols]


def tier_defaults(tier: str) -> tuple[str, str]:
    config = load_yaml(SCAN_POLICY_PATH).get("tiers", {}).get(tier, {})
    if not isinstance(config, dict):
        return "6mo", "1d"
    return str(config.get("period") or "6mo"), str(config.get("interval") or "1d")


def rolling(values: list[float], n: int) -> list[float]:
    return values[-n:] if len(values) >= n else values[:]


def avg(values: list[float]) -> float | None:
    return round(mean(values), 6) if values else None


def rsi(values: list[float], n: int = 14) -> float | None:
    if len(values) <= n:
        return None
    deltas = [values[index] - values[index - 1] for index in range(1, len(values))]
    recent = deltas[-n:]
    avg_gain = mean(max(delta, 0.0) for delta in recent)
    avg_loss = mean(abs(min(delta, 0.0)) for delta in recent)
    if avg_loss == 0:
        return 100.0
    ratio = avg_gain / avg_loss
    return round(100.0 - (100.0 / (1.0 + ratio)), 4)


def atr_pct(highs: list[float], lows: list[float], closes: list[float], n: int = 14) -> float | None:
    if len(closes) <= n or len(highs) != len(lows) or len(highs) != len(closes):
        return None
    ranges = []
    for index in range(1, len(closes)):
        ranges.append(max(highs[index] - lows[index], abs(highs[index] - closes[index - 1]), abs(lows[index] - closes[index - 1])))
    latest = closes[-1]
    return round((mean(ranges[-n:]) / latest) * 100.0, 4) if latest and ranges else None


def volume_z(volumes: list[float], n: int = 20) -> float | None:
    if len(volumes) < n + 1:
        return None
    baseline = volumes[-(n + 1) : -1]
    sigma = pstdev(baseline)
    return round((volumes[-1] - mean(baseline)) / sigma, 4) if sigma else None


def trend_label(close: float | None, sma20: float | None, sma60: float | None) -> str:
    if close is None or sma20 is None or sma60 is None:
        return "insufficient_data"
    if close > sma20 > sma60:
        return "uptrend"
    if close < sma20 < sma60:
        return "downtrend"
    if close > sma20 and sma20 < sma60:
        return "recovery_watch"
    if close < sma20 and sma20 > sma60:
        return "pullback_watch"
    return "mixed"


def feature_flags(
    closes: list[float],
    highs: list[float],
    lows: list[float],
    volumes: list[float],
    current_price: float,
    rsi14: float | None,
    vol_z20: float | None,
    live_return_pct: float | None,
    scan_tier: str,
) -> list[str]:
    flags: list[str] = []
    if len(highs) >= 21 and current_price >= max(highs[-20:]):
        flags.append("breakout_20d")
    if len(lows) >= 21 and current_price <= min(lows[-20:]):
        flags.append("breakdown_20d")
    ret5 = pct_change(current_price, closes[-6]) if len(closes) >= 6 else None
    if ret5 is not None and abs(ret5) >= 5:
        flags.append("strong_move_5d")
    if live_return_pct is not None and abs(live_return_pct) >= (1.5 if scan_tier == "core" else 4.0):
        flags.append("intraday_move")
    if vol_z20 is not None and vol_z20 >= (2.5 if scan_tier == "core" else 3.0):
        flags.append("volume_spike")
    if rsi14 is not None and rsi14 >= 70:
        flags.append("rsi_overbought")
    if rsi14 is not None and rsi14 <= 30:
        flags.append("rsi_oversold")
    return flags


def clean_series(frame: Any, name: str) -> list[float]:
    try:
        values = frame[name].dropna().tolist()
    except Exception:
        return []
    result = []
    for value in values:
        parsed = safe_float(value)
        if parsed is not None:
            result.append(parsed)
    return result


def build_features(symbol: str, frame: Any, live: dict[str, Any] | None, scan_tier: str, collected_at: str) -> dict[str, Any]:
    closes = clean_series(frame, "Close")
    highs = clean_series(frame, "High")
    lows = clean_series(frame, "Low")
    volumes = clean_series(frame, "Volume")
    if len(closes) < 3:
        raise RuntimeError("insufficient_close_data")
    historical_close = closes[-1]
    live_price = safe_float((live or {}).get("live_price"))
    current_price = live_price if live_price is not None else historical_close
    previous_close = safe_float((live or {}).get("previous_close"))
    if previous_close is None:
        previous_close = closes[-2] if len(closes) >= 2 else None
    live_return_pct = pct_change(current_price, previous_close)
    sma20 = avg(rolling(closes, 20))
    sma60 = avg(rolling(closes, 60))
    rsi14 = rsi(closes, 14)
    vol_z20 = volume_z(volumes, 20)
    flags = feature_flags(closes, highs, lows, volumes, current_price, rsi14, vol_z20, live_return_pct, scan_tier)
    source = str((live or {}).get("source") or "yfinance")
    source_tier = str((live or {}).get("source_tier") or "S2")
    return {
        "symbol": symbol,
        "scan_tier": scan_tier,
        "source": source,
        "history_source": "yfinance",
        "source_tier": source_tier,
        "market_data_type": (live or {}).get("market_data_type", "historical_aggregator"),
        "delayed": (live or {}).get("delayed"),
        "provider_update_time": (live or {}).get("provider_update_time", ""),
        "collected_at": str((live or {}).get("collected_at") or collected_at),
        "bars": len(closes),
        "last_close": round(current_price, 6),
        "history_last_close": round(historical_close, 6),
        "return_1d_pct": live_return_pct,
        "return_5d_pct": pct_change(current_price, closes[-6]) if len(closes) >= 6 else None,
        "return_20d_pct": pct_change(current_price, closes[-21]) if len(closes) >= 21 else None,
        "sma_20": sma20,
        "sma_60": sma60,
        "close_vs_sma20_pct": pct_change(current_price, sma20),
        "close_vs_sma60_pct": pct_change(current_price, sma60),
        "rsi_14": rsi14,
        "atr_14_pct": atr_pct(highs, lows, closes, 14),
        "volume_z_20": vol_z20,
        "trend_label": trend_label(current_price, sma20, sma60),
        "flags": flags,
        "price_resonance": any(flag in flags for flag in ("breakout_20d", "breakdown_20d", "volume_spike", "strong_move_5d", "intraday_move")),
    }


def fetch_history_frames(symbols: list[str], period: str, interval: str) -> dict[str, Any]:
    yf = optional_import("yfinance")
    data = yf.download(
        tickers=symbols,
        period=period,
        interval=interval,
        group_by="ticker",
        auto_adjust=False,
        threads=True,
        progress=False,
    )
    frames = {symbol: frame for symbol in symbols if (frame := select_symbol_frame(data, symbol, len(symbols))) is not None}
    missing = [symbol for symbol in symbols if len(clean_series(frames.get(symbol), "Close")) < 3]
    for symbol in missing:
        for attempt in range(2):
            try:
                retry = yf.download(
                    tickers=[symbol],
                    period=period,
                    interval=interval,
                    group_by="ticker",
                    auto_adjust=False,
                    threads=False,
                    progress=False,
                )
                frame = select_symbol_frame(retry, symbol, 1)
                if frame is not None and len(clean_series(frame, "Close")) >= 3:
                    frames[symbol] = frame
                    break
            except Exception:
                pass
            if attempt == 0:
                time.sleep(0.5)
    return frames


def merge_payload(existing: dict[str, Any], current: dict[str, Any]) -> dict[str, Any]:
    merged = {}
    for item in existing.get("items", []) if isinstance(existing, dict) else []:
        if isinstance(item, dict) and item.get("symbol"):
            merged[str(item["symbol"]).upper()] = item
    for item in current.get("items", []):
        merged[str(item["symbol"]).upper()] = item
    tier = str(current.get("scan_tier") or "manual")
    tier_status = dict(existing.get("tier_status") or {}) if isinstance(existing, dict) else {}
    tier_status[tier] = {
        "run_at": current.get("run_at"),
        "symbols": len(current.get("items") or []),
        "failures": len(current.get("failures") or []),
    }
    provider_status = dict(existing.get("provider_status") or {}) if isinstance(existing, dict) else {}
    provider_status.update(current.get("provider_status") or {})
    return {
        "version": "0.2",
        "run_at": current.get("run_at"),
        "period": "layered",
        "interval": "layered",
        "items": [merged[symbol] for symbol in sorted(merged)],
        "failures": current.get("failures") or [],
        "tier_status": tier_status,
        "provider_status": provider_status,
        "readonly": True,
        "order": False,
        "actual_broker_writes": False,
    }


def format_value(value: Any) -> str:
    if value is None:
        return "n/a"
    return f"{value:.2f}" if isinstance(value, float) else str(value)


def write_report(payload: dict[str, Any]) -> str:
    ensure_dir(REPORTS_DIR)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    path = REPORTS_DIR / f"kline_snapshot_{stamp}.md"
    lines = [
        "# K-Line Snapshot",
        "",
        f"- run_at: {payload['run_at']}",
        "- readonly: true",
        "- order: false",
        "",
        "| Symbol | Source | Data type | Trend | Price | 1D | 5D | RSI14 | VolZ20 | Flags |",
        "| --- | --- | --- | --- | ---: | ---: | ---: | ---: | ---: | --- |",
    ]
    for item in payload.get("items", []):
        lines.append(
            "| {symbol} | {source} | {kind} | {trend} | {price} | {r1} | {r5} | {rsi} | {volz} | {flags} |".format(
                symbol=item.get("symbol"), source=item.get("source"), kind=item.get("market_data_type"),
                trend=item.get("trend_label"), price=format_value(item.get("last_close")),
                r1=format_value(item.get("return_1d_pct")), r5=format_value(item.get("return_5d_pct")),
                rsi=format_value(item.get("rsi_14")), volz=format_value(item.get("volume_z_20")),
                flags=", ".join(item.get("flags") or []) or "none",
            )
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return str(path)


def parse_symbols(value: str, tier: str, max_symbols: int) -> list[str]:
    if value.strip():
        result = []
        for raw in value.split(","):
            symbol = raw.strip().upper()
            if symbol and symbol not in result:
                result.append(symbol)
        return result[:max_symbols]
    tier_symbols = symbols_from_tier(tier, max_symbols)
    return tier_symbols or symbols_from_watchlist(max_symbols)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbols", default="")
    parser.add_argument("--tier", default="manual")
    parser.add_argument("--period", default="")
    parser.add_argument("--interval", default="")
    parser.add_argument("--max-symbols", type=int, default=80)
    parser.add_argument("--output", default="")
    parser.add_argument("--replace-output", action="store_true")
    parser.add_argument("--no-live-snapshot", action="store_true")
    parser.add_argument("--write-report", action="store_true")
    args = parser.parse_args()

    ensure_dir(STATE_DIR)
    default_period, default_interval = tier_defaults(args.tier)
    period = args.period or default_period
    interval = args.interval or default_interval
    symbols = parse_symbols(args.symbols, args.tier, args.max_symbols)
    run_at = utc_now_iso()
    live_payload = {"items": [], "provider_status": {}}
    if not args.no_live_snapshot:
        live_payload = fetch_realtime_snapshots(symbols)
    live_by_symbol = {str(item.get("symbol")).upper(): item for item in live_payload.get("items", []) if isinstance(item, dict)}
    items: list[dict[str, Any]] = []
    failures: list[dict[str, str]] = []
    try:
        frames = fetch_history_frames(symbols, period, interval)
    except Exception as exc:
        frames = {}
        failures.append({"symbol": "*batch*", "error": exc.__class__.__name__})
    for symbol in symbols:
        frame = frames.get(symbol)
        if frame is None:
            failures.append({"symbol": symbol, "error": "history_unavailable"})
            continue
        try:
            items.append(build_features(symbol, frame, live_by_symbol.get(symbol), args.tier, run_at))
        except Exception as exc:
            failures.append({"symbol": symbol, "error": exc.__class__.__name__})

    current = {
        "version": "0.2",
        "run_at": run_at,
        "scan_tier": args.tier,
        "period": period,
        "interval": interval,
        "items": items,
        "failures": failures,
        "provider_status": live_payload.get("provider_status") or {},
        "readonly": True,
        "order": False,
        "actual_broker_writes": False,
    }
    latest_path = Path(args.output) if args.output else STATE_DIR / "kline_snapshot_latest.json"
    payload = current if args.replace_output else merge_payload(load_json(latest_path, {}), current)
    write_json(latest_path, payload)
    report = write_report(current) if args.write_report else "disabled"
    print(
        f"KLINE_SNAPSHOT_OK tier={args.tier} symbols={len(items)} failures={len(failures)} "
        f"state={latest_path} report={report} order=false"
    )
    return 0 if items else 2


if __name__ == "__main__":
    raise SystemExit(main())
