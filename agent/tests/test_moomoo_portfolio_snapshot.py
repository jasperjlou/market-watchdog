from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


AGENT_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = AGENT_ROOT / "scripts" / "moomoo_portfolio_snapshot.py"


def load_module():
    spec = importlib.util.spec_from_file_location("moomoo_portfolio_snapshot_test", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(SCRIPT.parent))
    try:
        spec.loader.exec_module(module)
    finally:
        sys.path.pop(0)
    return module


def test_position_rows_are_aggregated_without_account_identifiers() -> None:
    module = load_module()
    positions = module.aggregate_positions(
        [
            {"code": "US.NVDA", "qty": 2, "cost_price": 100, "nominal_price": 120, "market_val": 240, "unrealized_pl": 40, "can_sell_qty": 2},
            {"code": "US.NVDA", "qty": 1, "cost_price": 130, "nominal_price": 120, "market_val": 120, "unrealized_pl": -10, "can_sell_qty": 1},
        ]
    )

    assert positions == [
        {
            "symbol": "NVDA",
            "position": 3.0,
            "market_price": 120.0,
            "market_value": 360.0,
            "unrealized_pnl": 30.0,
            "can_sell_quantity": 3.0,
            "currency": "USD",
            "average_cost": 110.0,
        }
    ]


def test_unavailable_snapshot_is_safe_and_nonfatal() -> None:
    module = load_module()
    payload = module.unavailable("127.0.0.1", 11111, "opend_unavailable")

    assert payload["ok"] is False
    assert payload["actual_broker_writes"] is False
    assert payload["account_ids_persisted"] is False
    assert payload["positions"] == []
