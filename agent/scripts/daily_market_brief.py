#!/usr/bin/env python3
"""Create and optionally queue concise daily or weekly market outlooks."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable


PROJECT_DIR = Path(__file__).resolve().parents[2]
APP_DIR = Path(os.environ.get("APP_DIR", str(PROJECT_DIR)))
AGENT_DIR = Path(os.environ.get("AGENT_DIR", str(APP_DIR / "agent")))
STATE_DIR = AGENT_DIR / "state"
OUTLOOK_PATH = STATE_DIR / "trend_outlook_latest.json"
STATE_PATH = STATE_DIR / "daily_market_brief_state.json"
WEEKLY_STATE_PATH = STATE_DIR / "weekly_market_brief_state.json"
POLICY_PATH = APP_DIR / "config" / "trend_outlook_policy.yaml"
GATEWAY_PATH = AGENT_DIR / "scripts" / "communication_gateway.py"
REPORTS_DIR = APP_DIR / "reports"
CATEGORY_NAMES = {
    "stable_up": "平稳上涨",
    "stable_down": "平稳下跌",
    "range_stable": "震荡/平稳",
    "watch_up": "关注上涨",
    "watch_down": "关注下跌",
    "high_risk": "高风险",
    "insufficient_history": "样本不足",
}
CJK_RE = re.compile(r"[\u4e00-\u9fff]")
THEME_NAMES = {
    "memory_storage": "存储",
    "mega_cap_leaders": "大型科技",
    "space_aerospace": "航天航空",
    "broad_market": "大盘",
    "semiconductors": "半导体",
    "gold": "黄金",
}


def compact_text(value: Any, limit: int) -> str:
    text = " ".join(str(value or "").split()).strip("；。 ")
    if len(text) <= limit:
        return text
    return text[:limit].rstrip("，；。 ") + "…"


def chinese_news_text(item: dict[str, Any], direction: str) -> str:
    for key in ("summary_zh", "title_zh", "summary", "title"):
        candidate = " ".join(str(item.get(key) or "").split())
        if len(CJK_RE.findall(candidate)) >= 8:
            return compact_text(candidate, 72)
    tier = str(item.get("source_tier") or "S3").upper()
    raw_direction = str(item.get("direction") or "neutral")
    if tier not in {"S0", "S1"} or raw_direction not in {"bullish", "bearish", "mixed"}:
        return ""
    topics = " ".join(str(value).lower() for value in (item.get("topics") or []))
    theme = next((name for key, name in (
        ("memory", "存储"), ("semiconductor", "半导体"), ("space", "航天航空"),
        ("aerospace", "航天航空"), ("gold", "黄金"), ("broad_market", "大盘"),
        ("market", "大盘"),
    ) if key in topics), "相关板块")
    entities = [str(value).upper() for value in (item.get("entities") or []) if str(value).strip()][:3]
    entity_text = f"（{'、'.join(entities)}）" if entities else ""
    trust = "官方" if tier == "S0" else "高可信"
    horizon = {
        "1_5d": "短线", "short": "短线", "2_6w": "中期", "swing": "中期",
    }.get(str(item.get("impact_horizon") or ""), "后续")
    return f"{theme}{entity_text}出现{trust}{direction}消息，影响偏{horizon}"


def compact_news_lines(news: list[dict[str, Any]], limit: int) -> list[str]:
    lines: list[str] = []
    generic_groups: dict[str, dict[str, Any]] = {}
    theme_pairs = (
        ("memory", "存储"), ("semiconductor", "半导体"), ("space", "航天航空"),
        ("aerospace", "航天航空"), ("gold", "黄金"), ("broad_market", "大盘"),
        ("market", "大盘"),
    )
    for item in news:
        direction = {"bullish": "偏多", "bearish": "偏空", "mixed": "分化", "neutral": "中性"}.get(
            str(item.get("direction")), "待定"
        )
        text = chinese_news_text(item, direction)
        if not text:
            continue
        has_chinese = any(
            len(CJK_RE.findall(" ".join(str(item.get(key) or "").split()))) >= 8
            for key in ("summary_zh", "title_zh", "summary", "title")
        )
        if has_chinese:
            lines.append(text)
            continue
        topics = " ".join(str(value).lower() for value in (item.get("topics") or []))
        theme = next((name for key, name in theme_pairs if key in topics), "相关板块")
        group = generic_groups.setdefault(theme, {"items": [], "directions": set(), "entities": [], "official": False})
        group["items"].append(item)
        group["directions"].add(str(item.get("direction") or "neutral"))
        group["official"] = bool(group["official"]) or str(item.get("source_tier") or "").upper() == "S0"
        for entity in item.get("entities") or []:
            symbol = str(entity).upper().strip()
            if symbol and symbol not in group["entities"]:
                group["entities"].append(symbol)

    for theme, group in generic_groups.items():
        directions = group["directions"]
        if "bullish" in directions and "bearish" in directions:
            entities = group["entities"][:3]
            entity_text = f"（{'、'.join(entities)}）" if entities else ""
            trust = "官方及高可信" if group["official"] else "高可信"
            lines.append(f"{theme}{entity_text}{trust}消息多空分化，短线方向需等待价格确认")
        else:
            item = group["items"][0]
            direction = {"bullish": "偏多", "bearish": "偏空", "mixed": "分化", "neutral": "中性"}.get(
                str(item.get("direction")), "待定"
            )
            lines.append(chinese_news_text(item, direction))
    return lines[:limit]


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
    temporary.replace(path)


def as_eastern(value: datetime) -> datetime:
    utc_value = value.astimezone(timezone.utc)
    year = utc_value.year
    march_first = datetime(year, 3, 1, tzinfo=timezone.utc)
    second_sunday = 1 + ((6 - march_first.weekday()) % 7) + 7
    november_first = datetime(year, 11, 1, tzinfo=timezone.utc)
    first_sunday = 1 + ((6 - november_first.weekday()) % 7)
    dst_start = datetime(year, 3, second_sunday, 7, tzinfo=timezone.utc)
    dst_end = datetime(year, 11, first_sunday, 6, tzinfo=timezone.utc)
    hours = -4 if dst_start <= utc_value < dst_end else -5
    return utc_value.astimezone(timezone(timedelta(hours=hours), name="EDT" if hours == -4 else "EST"))


def market_date(now: datetime | None = None) -> str:
    return as_eastern(now or datetime.now(timezone.utc)).date().isoformat()


def market_week(now: datetime | None = None) -> str:
    iso = as_eastern(now or datetime.now(timezone.utc)).isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


def symbol_groups(items: list[dict[str, Any]]) -> dict[str, list[str]]:
    groups = {key: [] for key in CATEGORY_NAMES}
    for item in items:
        category = str(item.get("category") or "range_stable")
        groups.setdefault(category, []).append(str(item.get("symbol") or "?"))
    return groups


def compact_symbols(symbols: list[str], limit: int = 24) -> str:
    if not symbols:
        return "无"
    shown = symbols[:limit]
    suffix = f" 等{len(symbols)}只" if len(symbols) > limit else ""
    return ", ".join(shown) + suffix


def risk_item(item: dict[str, Any]) -> str:
    short = item.get("short_term") if isinstance(item.get("short_term"), dict) else {}
    return f"{item.get('symbol')}（{short.get('direction', '震荡')}，风险{item.get('risk_score', 0)}）"


def theme_item(item: dict[str, Any]) -> str:
    name = THEME_NAMES.get(str(item.get("theme") or ""), str(item.get("theme") or "相关主题"))
    value = item.get("median_return_5d_pct")
    suffix = "" if value is None else f"，5日{float(value):+.1f}%"
    return f"{name}{item.get('label', '平稳')}（{suffix.lstrip('，')}）" if suffix else f"{name}{item.get('label', '平稳')}"


def short_action(item: dict[str, Any]) -> str:
    if item.get("category") == "insufficient_history":
        return "样本不足，先观察"
    short = item.get("short_term") if isinstance(item.get("short_term"), dict) else {}
    direction = str(short.get("direction") or "震荡")
    if "空" in direction or "跌" in direction:
        return "先看能否止跌，未确认前不接"
    if "多" in direction or "涨" in direction:
        return "先看上涨能否延续，不追高"
    return "先等方向走出来"


def render_brief(outlook: dict[str, Any], policy: dict[str, Any], *, now: datetime | None = None) -> tuple[str, str]:
    current = now or datetime.now(timezone.utc)
    date = market_date(current)
    items = [item for item in (outlook.get("items") or []) if isinstance(item, dict)]
    groups = symbol_groups(items)
    brief_policy = policy.get("daily_brief") if isinstance(policy.get("daily_brief"), dict) else {}
    detail_limit = min(4, max(1, int(brief_policy.get("max_symbols_per_section", 4))))
    priority_items = [item for item in items if item.get("category") in {"high_risk", "insufficient_history", "watch_down", "watch_up"}]
    priority_items.sort(key=lambda item: (-int(item.get("risk_score") or 0), str(item.get("symbol") or "")))
    themes = [item for item in (outlook.get("themes") or []) if isinstance(item, dict)]
    news = [item for item in (outlook.get("news_digest") or []) if isinstance(item, dict)]
    max_news = min(2, max(0, int(brief_policy.get("max_news_items", 2))))
    portfolio_flags = [item for item in items if (item.get("portfolio") or {}).get("review") not in {None, "none"}]
    stable_count = sum(len(groups.get(key, [])) for key in ("stable_up", "stable_down", "range_stable"))
    watch_count = sum(len(groups.get(key, [])) for key in ("watch_up", "watch_down"))
    high_count = len(groups.get("high_risk", []))
    focus = priority_items[:detail_limit]
    theme_focus = sorted(
        themes,
        key=lambda item: abs(float(item.get("median_return_5d_pct") or item.get("short_score") or 0)),
        reverse=True,
    )[:4]

    lines = [
        f"市场日报｜{date}",
        "",
        "今日回顾：",
        f"整体：共{len(items)}只；平稳或震荡{stable_count}只，关注{watch_count}只，高风险{high_count}只。",
    ]
    if theme_focus:
        lines.append("主题：" + "；".join(theme_item(item) for item in theme_focus) + "。")
    if focus:
        lines.append("重点：" + "；".join(risk_item(item) for item in focus) + "。")
    else:
        lines.append("重点：暂无需要升级处理的风险项。")
    insufficient = groups.get("insufficient_history", [])
    if insufficient:
        lines.append(f"样本不足：{'、'.join(insufficient[:3])}，暂不作中期判断。")
    for text in compact_news_lines(news, max_news):
        lines.append(f"消息：{text}。")

    lines.extend(["", "明日展望："])
    if focus:
        for item in focus[:3]:
            short = item.get("short_term") if isinstance(item.get("short_term"), dict) else {}
            lines.append(f"- {item.get('symbol')}：短线{short.get('direction', '震荡')}，风险{item.get('risk_score', 0)}；{short_action(item)}。")
    else:
        lines.append("- 暂无升级信号，开盘先看缺口、量能和新消息。")

    lines.extend(["", "未来展望（2-6周）："])
    future_items = focus or sorted(items, key=lambda item: -int(item.get("risk_score") or 0))[:detail_limit]
    if future_items:
        future_parts = []
        for item in future_items:
            swing = item.get("swing") if isinstance(item.get("swing"), dict) else {}
            future_parts.append(f"{item.get('symbol')}{swing.get('direction', '震荡')}")
        lines.append("- " + "；".join(future_parts) + "。")
    else:
        lines.append("- 数据不足，等待下一批有效行情。")

    lines.extend(["", "风险与操作建议："])
    if portfolio_flags:
        held = "、".join(str(item.get("symbol")) for item in portfolio_flags[:detail_limit])
        lines.append(f"- 持仓先复核 {held} 的保护位；无仓位先等确认。")
    elif focus:
        lines.append("- 已有仓位先保护利润、控制损失；无仓位不追涨杀跌，等价格和可信消息确认。")
    else:
        lines.append("- 暂无紧急动作，继续观察价格、成交量和可信消息。")
    lines.append("- 只提供提醒，系统不自动下单。")

    body = "\n".join(lines)
    max_chars = max(500, int(brief_policy.get("max_body_chars", 900)))
    if len(body) > max_chars:
        # The bounded sections above should normally fit.  This final guard only
        # shortens unusually long provider text while preserving every heading.
        body = body[:max_chars].rstrip("，；。 \n") + "。"
    return f"【市场监控】{date} 趋势与风险日报", body


def numeric(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def weekly_symbol_item(item: dict[str, Any]) -> str:
    return f"{item.get('symbol', '?')}（5日{numeric(item.get('return_5d_pct')):+.1f}%）"


def render_weekly_brief(
    outlook: dict[str, Any], policy: dict[str, Any], *, now: datetime | None = None,
) -> tuple[str, str]:
    current = now or datetime.now(timezone.utc)
    week = market_week(current)
    items = [item for item in (outlook.get("items") or []) if isinstance(item, dict)]
    weekly_policy = policy.get("weekly_brief") if isinstance(policy.get("weekly_brief"), dict) else {}
    detail_limit = min(4, max(1, int(weekly_policy.get("max_symbols_per_section", 4))))
    max_news = min(2, max(0, int(weekly_policy.get("max_news_items", 2))))
    ranked = sorted(items, key=lambda item: numeric(item.get("return_5d_pct")), reverse=True)
    strongest = ranked[:detail_limit]
    weakest = list(reversed(ranked[-detail_limit:])) if ranked else []
    priority = [
        item for item in items
        if item.get("category") in {"high_risk", "watch_down", "watch_up", "insufficient_history"}
    ]
    priority.sort(key=lambda item: (-int(item.get("risk_score") or 0), str(item.get("symbol") or "")))
    themes = [item for item in (outlook.get("themes") or []) if isinstance(item, dict)]
    theme_focus = sorted(
        themes,
        key=lambda item: abs(numeric(item.get("median_return_5d_pct") or item.get("short_score"))),
        reverse=True,
    )[:4]
    news = [item for item in (outlook.get("news_digest") or []) if isinstance(item, dict)]
    groups = symbol_groups(items)
    stable_count = sum(len(groups.get(key, [])) for key in ("stable_up", "stable_down", "range_stable"))
    watch_count = sum(len(groups.get(key, [])) for key in ("watch_up", "watch_down"))

    lines = [
        f"市场周报｜{week}",
        "",
        "本周回顾：",
        f"整体：共{len(items)}只；平稳或震荡{stable_count}只，关注{watch_count}只，高风险{len(groups.get('high_risk', []))}只。",
    ]
    if strongest:
        lines.append("相对较强：" + "；".join(weekly_symbol_item(item) for item in strongest) + "。")
    if weakest:
        lines.append("相对较弱：" + "；".join(weekly_symbol_item(item) for item in weakest) + "。")
    if theme_focus:
        lines.append("板块表现：" + "；".join(theme_item(item) for item in theme_focus) + "。")
    for text in compact_news_lines(news, max_news):
        lines.append(f"消息：{text}。")

    lines.extend(["", "下周关注："])
    focus = priority[:detail_limit]
    if focus:
        for item in focus:
            short = item.get("short_term") if isinstance(item.get("short_term"), dict) else {}
            lines.append(
                f"- {item.get('symbol')}：短线{short.get('direction', '震荡')}，风险{item.get('risk_score', 0)}；{short_action(item)}。"
            )
    else:
        lines.append("- 暂无升级信号，重点观察开盘缺口、量能及新消息。")

    lines.extend(["", "未来展望（2-6周）："])
    future_items = focus or sorted(items, key=lambda item: -int(item.get("risk_score") or 0))[:detail_limit]
    if future_items:
        lines.append("- " + "；".join(
            f"{item.get('symbol')}{(item.get('swing') or {}).get('direction', '震荡')}"
            for item in future_items
        ) + "。")
    else:
        lines.append("- 数据不足，等待下一周有效行情。")

    lines.extend([
        "",
        "风险与操作建议：",
        "- 已有仓位优先保护利润并控制损失；无仓位不追涨杀跌，等待趋势和可信消息相互确认。",
        "- 只提供提醒，系统不自动下单。",
    ])
    body = "\n".join(lines)
    max_chars = max(600, int(weekly_policy.get("max_body_chars", 1100)))
    if len(body) > max_chars:
        body = body[:max_chars].rstrip("，；。 \n") + "。"
    return f"【市场监控】{week} 周度回顾与下周展望", body


def enqueue_via_gateway(subject: str, body: str) -> dict[str, Any]:
    command = [
        sys.executable, str(GATEWAY_PATH), "--enqueue",
        "--channel", "gmail", "--kind", "daily_brief", "--priority", "normal",
        "--subject", subject, "--body", body,
    ]
    try:
        proc = subprocess.run(command, cwd=str(APP_DIR), text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=30, check=False)
        return {"ok": proc.returncode == 0, "returncode": proc.returncode, "output_tail": proc.stdout[-500:]}
    except Exception as exc:
        return {"ok": False, "returncode": None, "error": exc.__class__.__name__}


def enqueue_weekly_via_gateway(subject: str, body: str) -> dict[str, Any]:
    command = [
        sys.executable, str(GATEWAY_PATH), "--enqueue",
        "--channel", "gmail", "--kind", "weekly_brief", "--priority", "normal",
        "--subject", subject, "--body", body,
    ]
    try:
        proc = subprocess.run(command, cwd=str(APP_DIR), text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=30, check=False)
        return {"ok": proc.returncode == 0, "returncode": proc.returncode, "output_tail": proc.stdout[-500:]}
    except Exception as exc:
        return {"ok": False, "returncode": None, "error": exc.__class__.__name__}


def run_daily_brief(
    outlook_path: Path, state_path: Path, policy_path: Path, *, enqueue: bool, force: bool,
    now: datetime | None = None, sender: Callable[[str, str], dict[str, Any]] = enqueue_via_gateway,
) -> dict[str, Any]:
    current = now or datetime.now(timezone.utc)
    date = market_date(current)
    state = load_json(state_path, {})
    if enqueue and not force and state.get("last_queued_market_date") == date:
        return {"ok": True, "skipped": True, "reason": "already_queued", "market_date": date}
    outlook = load_json(outlook_path, {"items": []})
    policy = load_yaml(policy_path)
    subject, body = render_brief(outlook, policy, now=current)
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    report_path = REPORTS_DIR / f"daily_market_brief_{date}.md"
    report_path.write_text(f"# {subject}\n\n{body}\n", encoding="utf-8")
    delivery = {"ok": True, "skipped": True, "reason": "preview_only"}
    next_state = dict(state) if isinstance(state, dict) else {}
    generated_at = current.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    next_state.update({
        "version": "1.0",
        "last_generated_market_date": date,
        "last_generated_at": generated_at,
        "last_delivery_status": "preview_only",
        "report_path": str(report_path),
        "actual_broker_writes": False,
    })
    if enqueue:
        delivery = sender(subject, body)
        next_state.update({
            "last_delivery_attempt_market_date": date,
            "last_delivery_attempt_at": generated_at,
            "last_delivery_status": "queued" if delivery.get("ok") else "failed",
        })
        if delivery.get("ok"):
            next_state.update({
                "last_queued_market_date": date,
                "last_queued_at": generated_at,
            })
    write_json(state_path, next_state)
    return {
        "ok": bool(delivery.get("ok")),
        "skipped": False,
        "market_date": date,
        "subject": subject,
        "body": body,
        "report_path": str(report_path),
        "delivery": delivery,
        "actual_broker_writes": False,
    }


def run_weekly_brief(
    outlook_path: Path, state_path: Path, policy_path: Path, *, enqueue: bool, force: bool,
    now: datetime | None = None, sender: Callable[[str, str], dict[str, Any]] = enqueue_weekly_via_gateway,
) -> dict[str, Any]:
    current = now or datetime.now(timezone.utc)
    week = market_week(current)
    state = load_json(state_path, {})
    if enqueue and not force and state.get("last_queued_week") == week:
        return {"ok": True, "skipped": True, "reason": "already_queued", "market_week": week}
    outlook = load_json(outlook_path, {"items": []})
    policy = load_yaml(policy_path)
    subject, body = render_weekly_brief(outlook, policy, now=current)
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    report_path = REPORTS_DIR / f"weekly_market_brief_{week}.md"
    report_path.write_text(f"# {subject}\n\n{body}\n", encoding="utf-8")
    delivery = {"ok": True, "skipped": True, "reason": "preview_only"}
    next_state = dict(state) if isinstance(state, dict) else {}
    generated_at = current.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    next_state.update({
        "version": "1.0",
        "last_generated_week": week,
        "last_generated_at": generated_at,
        "last_delivery_status": "preview_only",
        "report_path": str(report_path),
        "actual_broker_writes": False,
    })
    if enqueue:
        delivery = sender(subject, body)
        next_state.update({
            "last_delivery_attempt_week": week,
            "last_delivery_attempt_at": generated_at,
            "last_delivery_status": "queued" if delivery.get("ok") else "failed",
        })
        if delivery.get("ok"):
            next_state.update({"last_queued_week": week, "last_queued_at": generated_at})
    write_json(state_path, next_state)
    return {
        "ok": bool(delivery.get("ok")),
        "skipped": False,
        "market_week": week,
        "subject": subject,
        "body": body,
        "report_path": str(report_path),
        "delivery": delivery,
        "actual_broker_writes": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--outlook", default=str(OUTLOOK_PATH))
    parser.add_argument("--state", default="")
    parser.add_argument("--policy", default=str(POLICY_PATH))
    parser.add_argument("--enqueue", action="store_true")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--weekly", action="store_true")
    args = parser.parse_args()
    state_path = Path(args.state) if args.state else (WEEKLY_STATE_PATH if args.weekly else STATE_PATH)
    if args.weekly:
        result = run_weekly_brief(Path(args.outlook), state_path, Path(args.policy), enqueue=args.enqueue, force=args.force)
        period_name = "market_week"
        period_value = result[period_name]
        prefix = "WEEKLY_MARKET_BRIEF"
    else:
        result = run_daily_brief(Path(args.outlook), state_path, Path(args.policy), enqueue=args.enqueue, force=args.force)
        period_name = "market_date"
        period_value = result[period_name]
        prefix = "DAILY_MARKET_BRIEF"
    print(
        f"{prefix} "
        f"ok={str(result['ok']).lower()} skipped={str(result.get('skipped', False)).lower()} "
        f"{period_name}={period_value} actual_broker_writes=false"
    )
    return 0 if result["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
