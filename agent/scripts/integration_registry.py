#!/usr/bin/env python3
"""Read the sidecar integration registry for market-watchdog.

The registry is intentionally metadata-only. Full third-party applications
remain sidecars and are not installed into the IBKR runtime container.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any


APP_DIR = Path(os.environ.get("APP_DIR", "/app"))
AGENT_DIR = Path(os.environ.get("AGENT_DIR", str(APP_DIR / "agent")))
REGISTRY_PATH = AGENT_DIR / "integrations" / "registry.json"


def load_registry() -> dict[str, Any]:
    return json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--list", action="store_true", help="List all registered integrations.")
    parser.add_argument("--id", help="Show one integration by id.")
    parser.add_argument("--json", action="store_true", help="Print raw JSON.")
    args = parser.parse_args()

    registry = load_registry()

    if args.json:
        print(json.dumps(registry, ensure_ascii=False, indent=2))
        return 0

    items = registry.get("integrations", [])
    if args.id:
        matches = [item for item in items if item.get("id") == args.id]
        if not matches:
            print(f"integration not found: {args.id}", file=sys.stderr)
            return 2
        item = matches[0]
        print(f"{item['id']}: {item['name']}")
        print(f"  status: {item['status']}")
        print(f"  execution_mode: {item['execution_mode']}")
        print(f"  role: {item['role']}")
        print(f"  source: {item['source_repo']} @ {item['source_ref']}")
        print(f"  license: {item['license']}")
        print(f"  sidecar_path: {item['sidecar_path']}")
        return 0

    if args.list or not args.id:
        for item in items:
            print(f"{item['id']}|{item['status']}|{item['execution_mode']}|{item['name']}")
        return 0

    return 0


if __name__ == "__main__":
    sys.exit(main())
