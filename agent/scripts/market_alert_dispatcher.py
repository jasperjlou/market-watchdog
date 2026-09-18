#!/usr/bin/env python3
"""Queue new L2/L3/L4 fusion events for gated Telegram delivery."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable


APP_DIR = Path("/app")
AGENT_DIR = APP_DIR / "agent"
DRAFT_PATH = AGENT_DIR / "state" / "source_events_draft.json"
STATE_PATH = AGENT_DIR / "state" / "market_alert_dispatch_state.json"
GATEWAY_PATH = AGENT_DIR / "scripts" / "communication_gateway.py"
LEVEL_RANK = {"L0": 0, "L1": 1, "L2": 2, "L3": 3, "L4": 4}


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def load_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return default


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def event_key(item: dict[str, Any]) -> str:
    event_id = str(item.get("event_id") or "").strip()
    if not event_id:
        raw = "|".join(str(item.get(key) or "") for key in ("title", "url", "published_at"))
        event_id = "anon_" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
    return f"{event_id}:{str(item.get('alert_level') or 'L0')}"


def select_new_alerts(draft: dict[str, Any], state: dict[str, Any], min_level: str) -> list[dict[str, Any]]:
    minimum = LEVEL_RANK.get(min_level, LEVEL_RANK["L2"])
    queued = state.get("queued") if isinstance(state.get("queued"), dict) else {}
    selected: list[dict[str, Any]] = []
    for item in draft.get("items", []) if isinstance(draft, dict) else []:
        if not isinstance(item, dict):
            continue
        level = str(item.get("alert_level") or "L0")
        if LEVEL_RANK.get(level, 0) < minimum or event_key(item) in queued:
            continue
        selected.append(item)
    return selected


def alert_payload(item: dict[str, Any]) -> dict[str, str]:
    level = str(item.get("alert_level") or "L2")
    title = str(item.get("title") or "market event").strip()[:160]
    entities = "、".join(str(value) for value in (item.get("entities") or [])[:4]) or "相关标的"
    summary = " ".join(str(item.get("raw_summary") or item.get("freshness_reason") or "已完成复核").split())
    recommendation = " ".join(str(item.get("trade_recommendation") or "等待下一次价格和可信消息确认").split())
    trend = " ".join(str(
        item.get("price_trend")
        or item.get("market_resonance")
        or item.get("trend_conclusion")
        or "价格方向与量能仍需下一次快照确认"
    ).split())
    priority = {"L4": "urgent", "L3": "high", "L2": "normal"}.get(level, "normal")
    lines = [
        f"相关事件说明：{entities}｜{title}；{summary}"[:82],
        f"价格趋势：{trend}"[:62],
        f"操作建议：{recommendation}"[:72],
    ]
    body = "\n".join(line.rstrip("，；。 ") + "。" for line in lines)
    if len(body) > 220:
        body = body[:219].rstrip("，；。 \n") + "。"
    return {
        "channel": "telegram",
        "kind": "portfolio_hit" if level == "L4" else f"market_review_{level}",
        "priority": priority,
        "subject": f"[market-watchdog] {level} {title}",
        "body": body,
    }


def is_substantive_review(item: dict[str, Any]) -> bool:
    if str(item.get("ai_review_status") or "") not in {"complete", "complete_with_fallback"}:
        return False
    required = ("raw_summary", "trade_recommendation", "invalidation", "next_check")
    return all(len(str(item.get(key) or "").strip()) >= 8 for key in required)


def enqueue_via_gateway(alert: dict[str, str]) -> dict[str, Any]:
    command = [
        sys.executable,
        str(GATEWAY_PATH),
        "--enqueue",
        "--channel", alert["channel"],
        "--kind", alert["kind"],
        "--priority", alert["priority"],
        "--subject", alert["subject"],
        "--body", alert["body"],
    ]
    try:
        proc = subprocess.run(
            command,
            cwd=str(APP_DIR),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=30,
            check=False,
        )
        return {"ok": proc.returncode == 0, "returncode": proc.returncode, "output_tail": proc.stdout[-500:]}
    except Exception as exc:
        return {"ok": False, "returncode": None, "error": exc.__class__.__name__}


def dispatch_once(
    draft_path: Path,
    state_path: Path,
    *,
    min_level: str,
    enqueue: Callable[[dict[str, str]], dict[str, Any]] = enqueue_via_gateway,
) -> dict[str, Any]:
    draft = load_json(draft_path, {"items": []})
    state = load_json(state_path, {"queued": {}})
    if not isinstance(state, dict):
        state = {"queued": {}}
    queued = state.get("queued") if isinstance(state.get("queued"), dict) else {}
    selected = select_new_alerts(draft if isinstance(draft, dict) else {}, {"queued": queued}, min_level)
    results = []
    deferred = 0
    for item in selected:
        if not is_substantive_review(item):
            deferred += 1
            continue
        alert = alert_payload(item)
        result = enqueue(alert)
        results.append({"event_key": event_key(item), "ok": bool(result.get("ok"))})
        if result.get("ok"):
            queued[event_key(item)] = utc_now_iso()
    if len(queued) > 2000:
        queued = dict(list(queued.items())[-2000:])
    state = {
        "version": "1.0",
        "updated_at": utc_now_iso(),
        "min_level": min_level,
        "queued": queued,
        "last_results": results[-20:],
        "actual_broker_writes": False,
    }
    write_json(state_path, state)
    return {
        "selected_count": len(selected),
        "queued_count": sum(1 for item in results if item["ok"]),
        "failed_count": sum(1 for item in results if not item["ok"]),
        "deferred_count": deferred,
        "state_path": str(state_path),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--draft", default=str(DRAFT_PATH))
    parser.add_argument("--state", default=str(STATE_PATH))
    parser.add_argument("--min-level", choices=["L2", "L3", "L4"], default="L2")
    args = parser.parse_args()
    result = dispatch_once(Path(args.draft), Path(args.state), min_level=args.min_level)
    print(
        "MARKET_ALERT_DISPATCHER "
        f"selected={result['selected_count']} queued={result['queued_count']} "
        f"failed={result['failed_count']} actual_broker_writes=false"
    )
    return 0 if not result["failed_count"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
