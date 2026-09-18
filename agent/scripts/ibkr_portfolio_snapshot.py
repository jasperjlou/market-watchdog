#!/usr/bin/env python3
"""Write a read-only IBKR portfolio snapshot for market-watchdog.

This script connects to the local IB Gateway API in read-only mode, records
positions and open orders, and never calls broker write methods.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


APP_DIR = Path(os.environ.get("APP_DIR", "/app"))
AGENT_DIR = Path(os.environ.get("AGENT_DIR", str(APP_DIR / "agent")))
STATE_DIR = AGENT_DIR / "state"
DEFAULT_OUTPUT = STATE_DIR / "ibkr_portfolio_snapshot_latest.json"


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def account_hash(account: str) -> str:
    return hashlib.sha256(account.encode("utf-8")).hexdigest()[:12] if account else ""


def default_client_id() -> int:
    raw = os.environ.get("IBKR_PORTFOLIO_CLIENT_ID", "").strip()
    if raw:
        return int(raw)
    # Avoid fixed client-id collisions with the health check, hold client, or a
    # previous snapshot that is still being released by IB Gateway.
    return 900 + (os.getpid() % 80)


def classify_connectivity_code(code: int) -> str:
    return {
        1100: "connection_lost",
        1101: "reconnected_resubscribe_required",
        1102: "reconnected_data_maintained",
        1300: "socket_port_changed",
        2104: "data_farm_ready",
        2106: "historical_data_farm_ready",
        2158: "security_definition_farm_ready",
    }.get(int(code), "other")


def request_readonly_open_orders(ib: Any, timeout: float) -> list[dict[str, Any]]:
    """Read all API open orders without binding manual orders to this client."""
    ib.reqAllOpenOrders()
    ib.sleep(min(1, max(0.2, timeout / 10)))
    return [order_payload(trade) for trade in ib.openTrades()]


def contract_payload(contract: Any) -> dict[str, Any]:
    return {
        "symbol": getattr(contract, "symbol", None),
        "local_symbol": getattr(contract, "localSymbol", None),
        "sec_type": getattr(contract, "secType", None),
        "currency": getattr(contract, "currency", None),
        "exchange": getattr(contract, "exchange", None),
        "primary_exchange": getattr(contract, "primaryExchange", None),
        "con_id": getattr(contract, "conId", None),
    }


def order_payload(item: Any) -> dict[str, Any]:
    contract = getattr(item, "contract", None)
    order = getattr(item, "order", item)
    order_status = getattr(item, "orderStatus", None)
    return {
        "contract": contract_payload(contract) if contract is not None else {},
        "order_id": getattr(order, "orderId", None),
        "perm_id": getattr(order, "permId", None),
        "action": getattr(order, "action", None),
        "order_type": getattr(order, "orderType", None),
        "total_quantity": getattr(order, "totalQuantity", None),
        "filled_quantity": getattr(order_status, "filled", None) if order_status is not None else None,
        "remaining_quantity": getattr(order_status, "remaining", None) if order_status is not None else None,
        "limit_price": getattr(order, "lmtPrice", None),
        "aux_price": getattr(order, "auxPrice", None),
        "time_in_force": getattr(order, "tif", None),
        "outside_rth": getattr(order, "outsideRth", None),
        "status": getattr(order_status, "status", None) if order_status is not None else None,
        "transmit": getattr(order, "transmit", None),
    }


def collect_snapshot(host: str, port: int, client_id: int, timeout: float) -> dict[str, Any]:
    try:
        from ib_insync import IB  # type: ignore
    except Exception as exc:
        return {
            "version": "0.1",
            "ok": False,
            "collected_at": utc_now_iso(),
            "readonly": True,
            "error": f"ib_insync_unavailable:{exc.__class__.__name__}",
        }

    ib = IB()
    try:
        try:
            ib.RequestTimeout = timeout
        except Exception:
            pass
        ib.connect(host, port, clientId=client_id, timeout=timeout, readonly=True)
        server_time = None
        try:
            value = ib.reqCurrentTime()
            server_time = value.isoformat() if hasattr(value, "isoformat") else str(value)
        except Exception:
            server_time = None

        positions = []
        accounts = set()
        for pos in ib.positions():
            account = str(getattr(pos, "account", "") or "")
            if account:
                accounts.add(account)
            positions.append({
                "account_hash": account_hash(account),
                "contract": contract_payload(getattr(pos, "contract", None)),
                "position": getattr(pos, "position", None),
                "avg_cost": getattr(pos, "avgCost", None),
            })

        summary_items = {}
        wanted = {
            "NetLiquidation",
            "TotalCashValue",
            "AvailableFunds",
            "BuyingPower",
            "UnrealizedPnL",
            "RealizedPnL",
            "MaintMarginReq",
            "InitMarginReq",
            "ExcessLiquidity",
        }
        try:
            for item in ib.accountSummary():
                tag = getattr(item, "tag", "")
                if tag in wanted:
                    summary_items[tag] = {
                        "value": getattr(item, "value", None),
                        "currency": getattr(item, "currency", None),
                    }
        except Exception:
            summary_items = {}

        open_orders = []
        try:
            open_orders = request_readonly_open_orders(ib, timeout)
        except Exception:
            open_orders = []

        return {
            "version": "0.1",
            "ok": True,
            "collected_at": utc_now_iso(),
            "server_time": server_time,
            "readonly": True,
            "host": host,
            "port": port,
            "client_id": client_id,
            "account_count": len(accounts),
            "positions_count": len(positions),
            "open_orders_count": len(open_orders),
            "positions": positions,
            "account_summary": summary_items,
            "open_orders": open_orders,
            "actual_broker_writes": False,
        }
    except Exception as exc:
        return {
            "version": "0.1",
            "ok": False,
            "collected_at": utc_now_iso(),
            "readonly": True,
            "host": host,
            "port": port,
            "client_id": client_id,
            "error": f"{exc.__class__.__name__}:{exc}",
            "actual_broker_writes": False,
        }
    finally:
        try:
            ib.disconnect()
        except Exception:
            pass


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default=os.environ.get("IBKR_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("IBKR_PORT", "4001")))
    parser.add_argument("--client-id", type=int, default=default_client_id())
    parser.add_argument("--timeout", type=float, default=8.0)
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    payload = collect_snapshot(args.host, args.port, args.client_id, args.timeout)
    write_json(Path(args.output), payload)
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(
            "IBKR_PORTFOLIO_SNAPSHOT "
            f"ok={str(payload.get('ok')).lower()} "
            f"positions={payload.get('positions_count', 0)} "
            f"open_orders={payload.get('open_orders_count', 0)} "
            f"output={args.output}"
        )
    return 0 if payload.get("ok") else 2


if __name__ == "__main__":
    raise SystemExit(main())
