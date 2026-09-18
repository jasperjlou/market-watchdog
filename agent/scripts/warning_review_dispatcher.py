#!/usr/bin/env python3
"""Send substantive, reviewed warning-chamber updates and suppress repeats."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable


APP_DIR = Path(os.environ.get("APP_DIR", "/app"))
AGENT_DIR = Path(os.environ.get("AGENT_DIR", str(APP_DIR / "agent")))
STATE_DIR = AGENT_DIR / "state"
STATE_PATH = STATE_DIR / "market_emergency_state.json"
REVIEWS_DIR = STATE_DIR / "ai_bus" / "reviews"
OUTLOOK_PATH = STATE_DIR / "trend_outlook_latest.json"
PORTFOLIO_PATH = STATE_DIR / "moomoo_portfolio_snapshot_latest.json"
GATEWAY_PATH = AGENT_DIR / "scripts" / "communication_gateway.py"
POLICY_PATH = APP_DIR / "config" / "trend_outlook_policy.yaml"
CJK_RE = re.compile(r"[\u4e00-\u9fff]")
LEVEL_RANK = {"L0": 0, "L1": 1, "L2": 2, "L3": 3, "L4": 4}
INTERNAL_REVIEW_TERMS = ("工人", "worker", "模型", "证据列表", "复核过程", "模型状态", "evidence")


def utc_iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def load_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return default


def load_yaml(path: Path) -> dict[str, Any]:
    try:
        import yaml  # type: ignore
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {}
    except Exception:
        return {}


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def by_symbol(payload: dict[str, Any], key: str = "items") -> dict[str, dict[str, Any]]:
    return {
        str(item.get("symbol") or "").upper(): item
        for item in payload.get(key, []) if isinstance(item, dict) and item.get("symbol")
    }


def compact_text(value: Any, limit: int) -> str:
    text = " ".join(str(value or "").split()).strip("；。 ")
    if len(text) <= limit:
        return text
    return text[:limit].rstrip("，；。 ") + "…"


def public_text(value: Any, limit: int) -> str:
    text = " ".join(str(value or "").split())
    text = text.replace("非执行建议：", "").replace("非执行建议:", "")
    text = text.replace("已核验新闻", "公开消息").replace("已验证新闻", "公开消息")
    text = text.replace("新闻证据尚不足以确认", "暂未发现能够确认")
    text = text.replace("已验证证据不足", "暂未发现明确消息")
    segments = [segment.strip() for segment in re.split(r"[；。]", text) if segment.strip()]
    visible = [
        segment for segment in segments
        if not any(term.lower() in segment.lower() for term in INTERNAL_REVIEW_TERMS)
    ]
    return compact_text("；".join(visible), limit)


def number(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def pct_text(value: Any) -> str:
    parsed = number(value)
    if parsed is None:
        return "暂无"
    return f"{parsed:+.1f}".replace("+0.0", "0.0").replace("-0.0", "0.0")


def notification_policy(policy: dict[str, Any]) -> dict[str, Any]:
    configured = policy.get("notifications") if isinstance(policy.get("notifications"), dict) else {}
    return {
        "l2_min_risk_score": float(configured.get("l2_min_risk_score", 65)),
        "l2_min_abs_move_pct": float(configured.get("l2_min_abs_move_pct", 8)),
        "l2_risk_move_floor_pct": float(configured.get("l2_risk_move_floor_pct", 5)),
        "l2_channel": str(configured.get("l2_channel", "telegram")),
        "higher_level_channel": str(configured.get("higher_level_channel", "telegram")),
        "resolution_min_level": str(configured.get("resolution_min_level", "L3")),
    }


def portfolio_symbols(portfolio: dict[str, Any]) -> set[str]:
    return {
        str(item.get("symbol") or "").upper()
        for item in portfolio.get("positions", [])
        if isinstance(item, dict) and item.get("symbol")
    }


def should_notify(warning: dict[str, Any], outlook: dict[str, Any], portfolio: dict[str, Any], settings: dict[str, Any]) -> bool:
    level = str(warning.get("alert_level") or "L2")
    if LEVEL_RANK.get(level, 2) >= LEVEL_RANK["L3"]:
        return True
    risk = number(outlook.get("risk_score")) or 0.0
    move = abs(number(warning.get("move_pct")) or 0.0)
    symbol = str(warning.get("symbol") or "").upper()
    item_portfolio = outlook.get("portfolio") if isinstance(outlook.get("portfolio"), dict) else {}
    portfolio_impact = (
        symbol in portfolio_symbols(portfolio)
        or item_portfolio.get("review") not in {None, "none"}
    )
    return bool(
        portfolio_impact
        or move >= settings["l2_min_abs_move_pct"]
        or (
            risk >= settings["l2_min_risk_score"]
            and move >= settings["l2_risk_move_floor_pct"]
        )
    )


def has_chinese(value: Any, minimum: int = 4) -> bool:
    return len(CJK_RE.findall(str(value or ""))) >= minimum


def review_is_substantive(review: dict[str, Any]) -> bool:
    if review.get("status") not in {"complete", "complete_with_fallback"}:
        return False
    synthesis = review.get("synthesis") if isinstance(review.get("synthesis"), dict) else {}
    required = ("event", "summary", "recommendation", "invalidation", "next_check")
    return (
        all(len(str(synthesis.get(key) or "").strip()) >= 8 for key in required)
        and all(has_chinese(synthesis.get(key)) for key in ("event", "summary", "recommendation"))
    )


def review_signature(warning: dict[str, Any], synthesis: dict[str, Any]) -> str:
    material = "|".join(str(value or "").strip() for value in (
        warning.get("alert_level"), warning.get("direction"), synthesis.get("summary"),
        synthesis.get("recommendation"), synthesis.get("invalidation"),
    ))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:20]


def render_alert(
    warning: dict[str, Any], review: dict[str, Any], outlook: dict[str, Any],
    portfolio: dict[str, Any], settings: dict[str, Any] | None = None,
) -> dict[str, str]:
    symbol = str(warning.get("symbol") or "?")
    level = str(warning.get("alert_level") or "L2")
    configured = settings or notification_policy({})
    synthesis = review.get("synthesis") if isinstance(review.get("synthesis"), dict) else {}
    short = outlook.get("short_term") if isinstance(outlook.get("short_term"), dict) else {}
    event = public_text(synthesis.get("event"), 48)
    summary = public_text(synthesis.get("summary"), 42)
    event_line = event
    if summary and summary not in event and event not in summary:
        event_line = compact_text(f"{event}；{summary}", 88)
    recommendation = public_text(synthesis.get("recommendation"), 72)
    body = "\n".join([
        f"相关事件说明：{event_line or '价格出现异常波动，暂未发现明确的新消息'}。",
        f"价格趋势：{symbol} {pct_text(warning.get('move_pct'))}%；短线{short.get('direction', '待定')}；风险{outlook.get('risk_score', '暂无')}/100。",
        f"操作建议：{recommendation or '先控制风险，等待价格和可信消息确认'}；不自动下单。",
    ])
    return {
        "channel": configured["l2_channel"] if level == "L2" else configured["higher_level_channel"],
        "kind": f"market_review_{level}",
        "priority": "urgent" if level in {"L3", "L4"} else "high",
        "subject": f"【{level}警报】{symbol} {pct_text(warning.get('move_pct'))}%",
        "body": body,
    }


def render_resolution(warning: dict[str, Any], outlook: dict[str, Any], settings: dict[str, Any] | None = None) -> dict[str, str]:
    symbol = str(warning.get("symbol") or "?")
    level = str(warning.get("alert_level") or "L2")
    configured = settings or notification_policy({})
    short = outlook.get("short_term") if isinstance(outlook.get("short_term"), dict) else {}
    body = "\n".join([
        f"相关事件说明：{symbol} 连续两个快照回到平静区，解除即时警戒。",
        f"价格趋势：当前 {pct_text(warning.get('move_pct'))}%；短线{short.get('direction', '震荡')}；风险{outlook.get('risk_score', '暂无')}/100。",
        "操作建议：已有仓位继续按保护位管理；无仓位不要因解除警戒追价；不自动下单。",
    ])
    return {
        "channel": configured["higher_level_channel"], "kind": f"market_review_{level}", "priority": "normal",
        "subject": f"【市场警报】解除警戒 {symbol}", "body": body,
    }


def enqueue_via_gateway(alert: dict[str, str]) -> dict[str, Any]:
    command = [
        sys.executable, str(GATEWAY_PATH), "--enqueue", "--channel", alert["channel"],
        "--kind", alert["kind"], "--priority", alert["priority"],
        "--subject", alert["subject"], "--body", alert["body"],
    ]
    try:
        proc = subprocess.run(command, cwd=str(APP_DIR), text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=30, check=False)
        return {"ok": proc.returncode == 0, "returncode": proc.returncode, "output_tail": proc.stdout[-500:]}
    except Exception as exc:
        return {"ok": False, "returncode": None, "error": exc.__class__.__name__}


def dispatch_ready(
    state_path: Path, reviews_dir: Path, outlook_path: Path, portfolio_path: Path, *,
    enqueue: Callable[[dict[str, str]], dict[str, Any]] = enqueue_via_gateway,
    now: datetime | None = None,
    policy_path: Path | None = None,
) -> dict[str, Any]:
    current = now or datetime.now(timezone.utc)
    state = load_json(state_path, {})
    chamber = state.get("warning_chamber") if isinstance(state.get("warning_chamber"), dict) else {}
    outlooks = by_symbol(load_json(outlook_path, {"items": []}))
    portfolio = load_json(portfolio_path, {"positions": []})
    settings = notification_policy(load_yaml(policy_path or POLICY_PATH))
    queued = 0
    deferred = 0
    suppressed = 0
    results = []
    for symbol, warning in chamber.items():
        if not isinstance(warning, dict) or warning.get("status") not in {"research_pending", "resolution_pending"}:
            continue
        if warning.get("status") == "resolution_pending":
            level = str(warning.get("alert_level") or "L2")
            if LEVEL_RANK.get(level, 2) < LEVEL_RANK.get(settings["resolution_min_level"], 3):
                suppressed += 1
                warning.update({
                    "status": "resolved", "resolved_at": utc_iso(current),
                    "resolution_notified": False,
                    "user_notification_suppressed": "l2_resolution_daily_summary",
                })
                chamber[symbol] = warning
                results.append({"symbol": symbol, "ok": True, "resolution": True, "suppressed": True})
                continue
            alert = render_resolution(warning, outlooks.get(str(symbol).upper(), {}), settings)
            delivery = enqueue(alert)
            ok = bool(delivery.get("ok"))
            results.append({"symbol": symbol, "ok": ok, "resolution": True})
            if ok:
                queued += 1
                warning.update({
                    "status": "resolved", "resolved_at": utc_iso(current),
                    "notification_count": int(warning.get("notification_count") or 0) + 1,
                })
                chamber[symbol] = warning
            continue
        correlation_id = str(warning.get("correlation_id") or "")
        review = load_json(reviews_dir / f"{correlation_id}.json", {}) if correlation_id else {}
        if not review_is_substantive(review):
            deferred += 1
            continue
        synthesis = review["synthesis"]
        signature = review_signature(warning, synthesis)
        if warning.get("last_notification_signature") == signature:
            continue
        outlook = outlooks.get(str(symbol).upper(), {})
        if not should_notify(warning, outlook, portfolio, settings):
            suppressed += 1
            warning.update({
                "status": "active_warning",
                "last_reviewed_move_pct": warning.get("move_pct"),
                "last_review_signature": signature,
                "review_status": review.get("status"),
                "user_notification_suppressed": "below_external_threshold",
            })
            chamber[symbol] = warning
            results.append({"symbol": symbol, "ok": True, "suppressed": True})
            continue
        alert = render_alert(warning, review, outlook, portfolio, settings)
        delivery = enqueue(alert)
        ok = bool(delivery.get("ok"))
        results.append({"symbol": symbol, "ok": ok, "correlation_id": correlation_id})
        if not ok:
            continue
        queued += 1
        warning.update({
            "status": "active_warning",
            "last_notified_at": utc_iso(current),
            "last_reviewed_move_pct": warning.get("move_pct"),
            "last_notification_signature": signature,
            "notification_count": int(warning.get("notification_count") or 0) + 1,
            "review_status": review.get("status"),
        })
        chamber[symbol] = warning
    state["version"] = "2.0"
    state["updated_at"] = utc_iso(current)
    state["warning_chamber"] = chamber
    state["actual_broker_writes"] = False
    write_json(state_path, state)
    return {
        "queued_count": queued, "deferred_count": deferred, "suppressed_count": suppressed,
        "results": results, "actual_broker_writes": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", default=str(STATE_PATH))
    parser.add_argument("--reviews-dir", default=str(REVIEWS_DIR))
    parser.add_argument("--outlook", default=str(OUTLOOK_PATH))
    parser.add_argument("--portfolio", default=str(PORTFOLIO_PATH))
    args = parser.parse_args()
    result = dispatch_ready(Path(args.state), Path(args.reviews_dir), Path(args.outlook), Path(args.portfolio))
    print(
        f"WARNING_REVIEW_DISPATCHER queued={result['queued_count']} "
        f"deferred={result['deferred_count']} suppressed={result['suppressed_count']} "
        "actual_broker_writes=false"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
