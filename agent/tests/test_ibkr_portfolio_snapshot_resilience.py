from __future__ import annotations

import importlib.util
from pathlib import Path


AGENT_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = AGENT_ROOT / "scripts" / "ibkr_portfolio_snapshot.py"


def load_module():
    spec = importlib.util.spec_from_file_location("ibkr_portfolio_snapshot", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeIB:
    def __init__(self):
        self.all_open_orders_requested = 0
        self.open_orders_requested = 0
        self.slept = 0.0

    def reqAllOpenOrders(self):
        self.all_open_orders_requested += 1

    def reqOpenOrders(self):
        self.open_orders_requested += 1

    def sleep(self, seconds):
        self.slept = seconds

    def openTrades(self):
        return []


def test_readonly_snapshot_requests_all_open_orders_without_binding_manual_orders() -> None:
    module = load_module()
    ib = FakeIB()

    result = module.request_readonly_open_orders(ib, timeout=8.0)

    assert result == []
    assert ib.all_open_orders_requested == 1
    assert ib.open_orders_requested == 0


def test_official_connectivity_codes_are_classified_for_recovery() -> None:
    module = load_module()

    assert module.classify_connectivity_code(1100) == "connection_lost"
    assert module.classify_connectivity_code(1101) == "reconnected_resubscribe_required"
    assert module.classify_connectivity_code(1102) == "reconnected_data_maintained"
    assert module.classify_connectivity_code(1300) == "socket_port_changed"
    assert module.classify_connectivity_code(2104) == "data_farm_ready"
