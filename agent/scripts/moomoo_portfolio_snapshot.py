#!/usr/bin/env python3
"""Collect a read-only Moomoo US position snapshot through local OpenD.

Only account discovery and position-list queries are used.  The module has no
order, cancel, modify, or trade-unlock path and never persists account IDs.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import socket
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from market_data_router import frame_rows, optional_import, safe_float


APP_DIR = Path(os.environ.get("APP_DIR", "/app"))
DEFAULT_OUTPUT = APP_DIR / "agent" / "state" / "moomoo_portfolio_snapshot_latest.json"


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.chmod(0o600)
    os.replace(temporary, path)
    path.chmod(0o600)


def port_open(host: str, port: int, timeout: float) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def enum_name(value: Any) -> str:
    raw = str(value or "").upper()
    return raw.rsplit(".", 1)[-1]


def positive_finite(value: Any) -> float | None:
    parsed = safe_float(value)
    return parsed if parsed is not None and math.isfinite(parsed) else None


def normalize_symbol(code: Any) -> str:
    raw = str(code or "").strip().upper()
    return raw[3:] if raw.startswith("US.") else raw


def aggregate_positions(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    aggregated: dict[str, dict[str, Any]] = {}
    for row in rows:
        symbol = normalize_symbol(row.get("code"))
        quantity = positive_finite(row.get("qty"))
        if not symbol or quantity is None or quantity == 0:
            continue
        average_cost = positive_finite(row.get("cost_price"))
        current_price = positive_finite(row.get("nominal_price"))
        market_value = positive_finite(row.get("market_val"))
        unrealized_pnl = positive_finite(row.get("unrealized_pl"))
        if unrealized_pnl is None:
            unrealized_pnl = positive_finite(row.get("pl_val"))
        can_sell = positive_finite(row.get("can_sell_qty"))
        item = aggregated.setdefault(
            symbol,
            {
                "symbol": symbol,
                "position": 0.0,
                "average_cost_numerator": 0.0,
                "average_cost_denominator": 0.0,
                "market_price": current_price,
                "market_value": 0.0,
                "unrealized_pnl": 0.0,
                "can_sell_quantity": 0.0,
                "currency": str(row.get("currency") or "USD"),
            },
        )
        item["position"] += quantity
        if average_cost is not None:
            item["average_cost_numerator"] += average_cost * abs(quantity)
            item["average_cost_denominator"] += abs(quantity)
        if market_value is not None:
            item["market_value"] += market_value
        if unrealized_pnl is not None:
            item["unrealized_pnl"] += unrealized_pnl
        if can_sell is not None:
            item["can_sell_quantity"] += can_sell
        if current_price is not None:
            item["market_price"] = current_price

    result = []
    for symbol in sorted(aggregated):
        item = aggregated[symbol]
        denominator = float(item.pop("average_cost_denominator"))
        numerator = float(item.pop("average_cost_numerator"))
        item["average_cost"] = round(numerator / denominator, 6) if denominator else None
        for key in ("position", "market_price", "market_value", "unrealized_pnl", "can_sell_quantity"):
            if item.get(key) is not None:
                item[key] = round(float(item[key]), 6)
        result.append(item)
    return result


def unavailable(host: str, port: int, reason: str) -> dict[str, Any]:
    return {
        "version": "1.0",
        "ok": False,
        "provider": "moomoo",
        "collected_at": utc_now_iso(),
        "host": host,
        "port": port,
        "reason": reason,
        "account_data_available": False,
        "positions_count": 0,
        "positions": [],
        "readonly": True,
        "actual_broker_writes": False,
        "account_ids_persisted": False,
    }


def collect_snapshot(host: str, port: int, timeout: float) -> dict[str, Any]:
    if not port_open(host, port, timeout):
        return unavailable(host, port, "opend_unavailable")
    try:
        api = optional_import("moomoo")
    except Exception as exc:
        return unavailable(host, port, f"module_unavailable:{exc.__class__.__name__}")
    if hasattr(api, "SysConfig") and hasattr(api.SysConfig, "enable_console_log"):
        api.SysConfig.enable_console_log(False)
    context = None
    try:
        context = api.OpenSecTradeContext(
            filter_trdmarket=api.TrdMarket.US,
            host=host,
            port=port,
            is_encrypt=None,
            security_firm=api.SecurityFirm.FUTUINC,
        )
        ret, accounts = context.get_acc_list()
        if ret != getattr(api, "RET_OK", 0):
            return unavailable(host, port, "account_list_query_failed")
        real_accounts = [
            row for row in frame_rows(accounts)
            if enum_name(row.get("trd_env")) == "REAL" and row.get("acc_id") is not None
        ]
        if not real_accounts:
            return unavailable(host, port, "no_real_us_account")
        raw_positions: list[dict[str, Any]] = []
        successful_accounts = 0
        for account in real_accounts:
            try:
                account_id = int(account["acc_id"])
            except (KeyError, TypeError, ValueError):
                continue
            ret, positions = context.position_list_query(
                trd_env=api.TrdEnv.REAL,
                acc_id=account_id,
                refresh_cache=False,
            )
            if ret == getattr(api, "RET_OK", 0):
                raw_positions.extend(frame_rows(positions))
                successful_accounts += 1
        if successful_accounts == 0:
            return unavailable(host, port, "position_list_query_failed")
        positions = aggregate_positions(raw_positions)
        return {
            "version": "1.0",
            "ok": True,
            "provider": "moomoo",
            "collected_at": utc_now_iso(),
            "host": host,
            "port": port,
            "account_data_available": True,
            "account_count": successful_accounts,
            "positions_count": len(positions),
            "positions": positions,
            "readonly": True,
            "query_methods": ["get_acc_list", "position_list_query"],
            "actual_broker_writes": False,
            "account_ids_persisted": False,
        }
    except Exception as exc:
        return unavailable(host, port, f"provider_error:{exc.__class__.__name__}")
    finally:
        if context is not None:
            try:
                context.close()
            except Exception:
                pass


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default=os.environ.get("MOOMOO_OPEND_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("MOOMOO_OPEND_PORT", "11111")))
    parser.add_argument("--timeout", type=float, default=2.0)
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    payload = collect_snapshot(args.host, args.port, args.timeout)
    write_json(Path(args.output), payload)
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(
            "MOOMOO_PORTFOLIO_SNAPSHOT "
            f"ok={str(payload.get('ok')).lower()} positions={payload.get('positions_count', 0)} "
            "readonly=true broker_writes=false account_ids_persisted=false"
        )
    # OpenD being logged out is a handled data-availability state.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
