#!/usr/bin/env python3
"""Return isolated Codex query results to the channel that originated them."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable


PROJECT_DIR = Path(__file__).resolve().parents[2]
APP_DIR = Path(os.environ.get("APP_DIR", str(PROJECT_DIR)))
AGENT_DIR = Path(os.environ.get("AGENT_DIR", str(APP_DIR / "agent")))
CONFIG_PATH = AGENT_DIR / "config" / "wechat_ai_gateway.json"
STATE_PATH = AGENT_DIR / "state" / "wechat_ai_result_dispatcher_state.json"
LATEST_ANSWER_PATH = AGENT_DIR / "state" / "wechat_ai_latest_answer.json"
TELEGRAM_LATEST_ANSWER_PATH = AGENT_DIR / "state" / "telegram_ai_latest_answer.json"
GATEWAY_PATH = AGENT_DIR / "scripts" / "communication_gateway.py"
BRIDGE_ROOT = Path("/var/lib/market-watchdog/codex-queries")
OUTBOX_DIR = BRIDGE_ROOT / "outbox"
DISPATCHED_DIR = BRIDGE_ROOT / "dispatched"


ACTION_ZH = {
    "hold": "持有",
    "watch": "观察",
    "reduce": "分批减持",
    "protect": "保护利润/控制风险",
    "wait_for_data": "等待数据",
}


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def load_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return default


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.chmod(0o600)
    os.replace(temporary, path)
    path.chmod(0o600)


def compact(value: Any, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit]


def clause(value: Any, limit: int) -> str:
    return compact(value, limit).rstrip("。；;，,")


def render_answer(payload: dict[str, Any]) -> tuple[str, bool]:
    answer = payload.get("answer") if isinstance(payload.get("answer"), dict) else {}
    portfolio_status = str(answer.get("portfolio_data_status") or "missing")
    if payload.get("request_type") == "portfolio_advice" and portfolio_status == "missing":
        body = (
            "【Codex 投资问答】\n"
            "结论：目前没有你的真实持仓，不能判断该减持哪些股票，也不能给出可靠卖价。\n"
            "操作建议：请发送股票代码、数量、成本和计划减持比例；或完成 Moomoo 持仓接入。收到后再结合趋势与新闻计算。\n"
            "人工确认，不会自动下单。"
        )
        return body, False
    lines = ["【Codex 投资问答】", f"结论：{clause(answer.get('summary'), 180)}"]
    if portfolio_status != "available":
        lines.append("持仓数据：不完整，以下仅分析指定股票，不判断你的真实仓位。")
    urgent = False
    for item in list(answer.get("candidates") or [])[:3]:
        if not isinstance(item, dict):
            continue
        symbol = compact(item.get("symbol"), 16) or "标的"
        action = str(item.get("action") or "wait_for_data")
        if action == "protect" and str(answer.get("confidence")) == "high":
            urgent = True
        lines.append(
            f"{symbol}：{ACTION_ZH.get(action, action)}；参考区间 {clause(item.get('sell_zone'), 80)}；"
            f"比例 {clause(item.get('trim_fraction'), 60)}。"
        )
        lines.append(
            f"依据：{clause(item.get('reason'), 120)}；失效条件：{clause(item.get('invalidation'), 90)}。"
        )
    if not answer.get("candidates"):
        lines.append("操作建议：数据不足，暂不生成具体减持名单或卖出价格。")
    lines.append(f"风险：{clause(answer.get('risk_note'), 120)}。")
    lines.append(f"数据时间：{compact(answer.get('data_as_of'), 80)}；置信度：{compact(answer.get('confidence'), 20)}")
    lines.append("人工确认，不会自动下单。")
    body = "\n".join(line for line in lines if not line.endswith("："))[:1600]
    return body, urgent


def render_unavailable(payload: dict[str, Any]) -> str:
    error = compact(payload.get("error"), 60) or "provider_error"
    return (
        "【Codex 投资问答】\n"
        "结论：Codex 暂时不可用，本次没有生成股票名单或参考卖价。\n"
        f"状态：{error}；请稍后重新发送问题。\n"
        "操作建议：不要依据本条消息交易。\n"
        "人工确认，不会自动下单。"
    )


def enqueue_message(channel: str, subject: str, body: str, priority: str) -> dict[str, Any]:
    proc = subprocess.run(
        [
            sys.executable, str(GATEWAY_PATH), "--enqueue", "--channel", channel,
            "--kind", "wechat_ai_reply", "--priority", priority,
            "--subject", subject, "--body", body,
        ],
        cwd=str(APP_DIR), text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        timeout=30, check=False,
    )
    return {"ok": proc.returncode == 0, "returncode": proc.returncode}


def dispatch_result(
    path: Path,
    enqueue: Callable[[str, str, str, str], dict[str, Any]] = enqueue_message,
) -> dict[str, Any]:
    payload = load_json(path, {})
    if not isinstance(payload, dict) or payload.get("actual_broker_writes") is not False:
        return {"ok": False, "error": "invalid_result", "path": str(path)}
    if payload.get("ok") is True and isinstance(payload.get("answer"), dict):
        body, urgent = render_answer(payload)
    elif payload.get("ok") is False:
        body, urgent = render_unavailable(payload), False
    else:
        return {"ok": False, "error": "invalid_result", "path": str(path)}
    config = load_json(CONFIG_PATH, {})
    delivery = payload.get("delivery") if isinstance(payload.get("delivery"), dict) else {}
    channel = str(delivery.get("source_channel") or delivery.get("primary_channel") or "wechat").strip().lower()
    if channel not in {"wechat", "telegram"}:
        return {"ok": False, "error": "invalid_source_channel", "path": str(path)}
    symbols = ",".join(str(value) for value in payload.get("symbols", []) if str(value)) or "持仓"
    externally_queued = False
    if channel == "telegram":
        queued = enqueue("telegram", f"【投资问答】{symbols}", body, "urgent" if urgent else "high")
        if not queued.get("ok"):
            return {"ok": False, "error": "enqueue_failed", "path": str(path)}
        externally_queued = True
        latest_path = TELEGRAM_LATEST_ANSWER_PATH
        reply_mode = "direct"
    else:
        # This unverified personal Official Account currently lacks proactive
        # customer-message permission (48001). Keep the answer on the WeChat
        # path and expose it only when the same user replies “结果”.
        latest_path = LATEST_ANSWER_PATH
        reply_mode = "passive_result_cache"
    write_json(
        latest_path,
        {
            "version": "1.0",
            "updated_at": utc_now_iso(),
            "job_id": payload.get("job_id"),
            "model": payload.get("model"),
            "symbols": payload.get("symbols", []),
            "body": body,
            "wechat_reply": body[:1600],
            "source_channel": channel,
            "reply_mode": reply_mode,
            "actual_broker_writes": False,
        },
    )
    DISPATCHED_DIR.mkdir(parents=True, exist_ok=True)
    shutil.move(str(path), str(DISPATCHED_DIR / path.name))
    return {
        "ok": True,
        "job_id": payload.get("job_id"),
        "channel": channel,
        "reply_mode": reply_mode,
        "externally_queued": externally_queued,
        "urgent": urgent,
    }


def run_once(limit: int) -> dict[str, Any]:
    results = [dispatch_result(path) for path in sorted(OUTBOX_DIR.glob("wechat_*.json"))[:max(1, limit)]]
    payload = {
        "version": "1.0",
        "updated_at": utc_now_iso(),
        "processed": len(results),
        "queued": sum(1 for item in results if item.get("ok")),
        "external_queued": sum(1 for item in results if item.get("externally_queued")),
        "failed": sum(1 for item in results if not item.get("ok")),
        "results": results[-20:],
        "actual_broker_writes": False,
    }
    write_json(STATE_PATH, payload)
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--limit", type=int, default=5)
    args = parser.parse_args()
    result = run_once(args.limit)
    print(
        f"AI_RESULT_DISPATCHER processed={result['processed']} completed={result['queued']} "
        f"external_queued={result['external_queued']} failed={result['failed']} broker_writes=false"
    )
    return 0 if result["failed"] == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
